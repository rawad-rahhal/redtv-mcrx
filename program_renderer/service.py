"""program_renderer.service (v4.14)
===================================
Persistent program renderer with *seamless source switching* (no encoder restart).

Key idea (v1 milestone)
-----------------------
- Keep ONE long-lived FFmpeg *encoder* process running, ingesting raw frames from stdin.
- Run lightweight decoder workers that produce frames for:
    * automation source (current playlist item file)
    * warm automation previews (NEXT, NEXT2)
    * live sources (per-slot decoder pool: cam_1..cam_4) so switching live slots
      does NOT restart the encoder *or* restart the live decoder.
- Switching sources does NOT restart the encoder. The compositor simply changes which
  decoded frame is written into the encoder stdin stream.

Notes
-----
- This implementation requires optional deps: numpy + Pillow (install extra: [media]).
  If missing, the service will run in "restart" mode (legacy fallback).
- Automation decoders may be restarted when the clip changes (acceptable). The
  milestone is NO encoder restart for switching between automation/live *and*
  instant switching across live slots.

Windows-first assumptions:
- FFmpeg in PATH (or config.redtv.yaml program_renderer.ffmpeg_path).
- UDP program output in config.redtv.yaml program_renderer.udp_out

Endpoints
---------
GET /health
GET /status
GET /preview/program.jpg
POST /control/restart   (forces encoder restart; should be rare)
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI
from fastapi.responses import Response, JSONResponse

log = logging.getLogger("redtv.program_renderer")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "redtv.yaml"

def _load_cfg() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

CFG = _load_cfg()
GW = (CFG.get("api_gateway") or {})
RCFG = (CFG.get("program_renderer") or {})

GW_BASE = (GW.get("base_url") or "http://127.0.0.1:8000").rstrip("/")
LIVE_BASE = ((CFG.get("live_ingest") or {}).get("base_url") or "http://127.0.0.1:8020").rstrip("/")

FFMPEG = str(RCFG.get("ffmpeg_path") or "ffmpeg")
UDP_OUT = str(RCFG.get("udp_out") or "udp://127.0.0.1:10000?pkt_size=1316")
PREVIEW_PATH = Path(RCFG.get("preview_path") or (Path(__file__).resolve().parent / "_preview" / "program.jpg"))
PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)

OVERLAY_DIR = Path(RCFG.get("overlay_dir") or (Path(__file__).resolve().parent / "_overlay"))
OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
BUG_TEXT_PATH = OVERLAY_DIR / "bug.txt"
L3_TEXT_PATH  = OVERLAY_DIR / "l3.txt"

FONT_PATH = str(RCFG.get("font_path") or "")
VIDEO_W = int(RCFG.get("width") or 1920)
VIDEO_H = int(RCFG.get("height") or 1080)
FPS = int(RCFG.get("fps") or 25)

# optional deps
try:
    import numpy as np  # type: ignore
    from PIL import Image  # type: ignore
    _MEDIA_OK = True
except Exception:
    np = None  # type: ignore
    Image = None  # type: ignore
    _MEDIA_OK = False

app = FastAPI(title="RED TV Program Renderer", version="4.16.2")


def _http_json(url: str, timeout: float = 1.2) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not t.endswith("\n"):
        t += "\n"
    tmp.write_text(t, encoding="utf-8")
    os.replace(tmp, path)


def _build_overlay_filter() -> str:
    # BUG top-right, L3 bottom
    draw_bug = ""
    draw_l3 = ""
    if BUG_TEXT_PATH:
        draw_bug = (
            f"drawtext=textfile='{BUG_TEXT_PATH.as_posix()}':reload=1:"
            f"x=w-tw-40:y=40:fontsize=42:fontcolor=white:box=1:boxcolor=0x00000099"
        )
    if L3_TEXT_PATH:
        draw_l3 = (
            f"drawtext=textfile='{L3_TEXT_PATH.as_posix()}':reload=1:"
            f"x=60:y=h-140:fontsize=54:fontcolor=white:box=1:boxcolor=0x00000099"
        )
    if FONT_PATH:
        if draw_bug:
            draw_bug += f":fontfile='{FONT_PATH}'"
        if draw_l3:
            draw_l3 += f":fontfile='{FONT_PATH}'"
    flt = ",".join([x for x in [draw_bug, draw_l3] if x])
    return flt or "null"


@dataclass
class _EncoderState:
    running: bool = False
    mode: str = "persistent"  # persistent | restart_fallback
    encoder_pid: Optional[int] = None
    encoder_restarts: int = 0
    last_frame_ts: float = 0.0
    last_error: str = ""
    selected_source: str = "automation"  # automation | live
    selected_live_slot: Optional[str] = None
    automation_path: str = ""
    next_automation_path: str = ""
    # PVW (Preview) selection
    pvw_mode: str = "live"  # live | next_auto | program
    pvw_live_slot: str = "cam_1"
    last_pvw_ts: float = 0.0
    # switching telemetry
    switch_count: int = 0
    last_switch_ts: float = 0.0
    # decoder telemetry
    dec_aut_restarts: int = 0
    # live slot decoder pool telemetry
    dec_live_restarts_total: int = 0
    dec_next_restarts: int = 0
    dec_next2_restarts: int = 0
    started_ts: float = 0.0


_STATE = _EncoderState()
_LOCK = threading.Lock()


class _JpegStreamReader:
    """Read JPEG frames from a subprocess stdout stream."""
    def __init__(self) -> None:
        self.last_jpeg: Optional[bytes] = None
        self.last_ts: float = 0.0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self, stream) -> None:
        buf = bytearray()
        soi = b"\xff\xd8"
        eoi = b"\xff\xd9"
        while not self._stop:
            chunk = stream.read(4096)
            if not chunk:
                time.sleep(0.01)
                continue
            buf.extend(chunk)
            while True:
                s = buf.find(soi)
                if s < 0:
                    if len(buf) > 2_000_000:
                        buf[:] = buf[-200_000:]
                    break
                e = buf.find(eoi, s + 2)
                if e < 0:
                    if s > 0:
                        del buf[:s]
                    break
                frame = bytes(buf[s : e + 2])
                del buf[: e + 2]
                self.last_jpeg = frame
                self.last_ts = time.time()


class _DecoderProc:
    def __init__(self, name: str) -> None:
        self.name = name
        self.proc: Optional[subprocess.Popen] = None
        self.reader = _JpegStreamReader()
        self.thread: Optional[threading.Thread] = None
        self.input_desc: str = ""
        self.restart_count: int = 0
        self.last_start_ts: float = 0.0
        self.last_error: str = ""

    def start(self, ffmpeg: str, input_url: str, w: int, h: int, fps: int) -> None:
        self.stop()
        self.input_desc = input_url
        self.restart_count += 1
        self.last_start_ts = time.time()
        self.last_error = ""
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-re",
            "-i", input_url,
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ]
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=creation)
        except Exception as e:
            self.proc = None
            self.last_error = str(e)
            raise
        self.reader = _JpegStreamReader()
        self.thread = threading.Thread(target=self.reader.run, args=(self.proc.stdout,), daemon=True)  # type: ignore[arg-type]
        self.thread.start()

    def stop(self) -> None:
        self.reader.stop()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.thread = None
        self.input_desc = ""


_DEC_AUT = _DecoderProc("automation")
_DEC_NEXT = _DecoderProc("automation_next")
_DEC_NEXT2 = _DecoderProc("automation_next2")

# v4.14: keep a per-slot live decoder pool so switching between cam_1..cam_4
# does NOT restart the encoder and does NOT restart the live decoder.
_LIVE_SLOT_IDS = [str(x) for x in (RCFG.get("live_slots") or ["cam_1", "cam_2", "cam_3", "cam_4"])][:4]
_DEC_LIVE_SLOTS: Dict[str, _DecoderProc] = {sid: _DecoderProc(f"live_{sid}") for sid in _LIVE_SLOT_IDS}

_ENCODER_PROC: Optional[subprocess.Popen] = None
_COMPOSITOR_THREAD: Optional[threading.Thread] = None
_POLL_THREAD: Optional[threading.Thread] = None
_STOP = False


def _start_encoder() -> None:
    global _ENCODER_PROC
    overlay = _build_overlay_filter()
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{VIDEO_W}x{VIDEO_H}",
        "-r", str(FPS),
        "-i", "pipe:0",
        "-vf", overlay,
        "-c:v", "libx264",
        "-preset", str(RCFG.get("x264_preset") or "veryfast"),
        "-tune", "zerolatency",
        "-b:v", str(RCFG.get("bitrate") or "6000k"),
        "-maxrate", str(RCFG.get("maxrate") or "6000k"),
        "-bufsize", str(RCFG.get("bufsize") or "12000k"),
        "-g", str(RCFG.get("gop") or (FPS*2)),
        "-f", "mpegts",
        UDP_OUT,
    ]
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _ENCODER_PROC = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0, creationflags=creation)
    with _LOCK:
        _STATE.running = True
        _STATE.mode = "persistent" if _MEDIA_OK else "restart_fallback"
        _STATE.encoder_pid = _ENCODER_PROC.pid
        _STATE.encoder_restarts += 1
        _STATE.started_ts = time.time()
        _STATE.last_error = ""


def _stop_encoder() -> None:
    global _ENCODER_PROC
    p = _ENCODER_PROC
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=2.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _ENCODER_PROC = None
    with _LOCK:
        _STATE.running = False
        _STATE.encoder_pid = None


def _poll_gateway_loop() -> None:
    global _DEC_AUT, _DEC_NEXT, _DEC_NEXT2
    # periodically update overlay text files and decoder inputs
    last_item = ""
    last_next = ""
    last_next2 = ""
    last_live_urls: Dict[str, str] = {sid: "" for sid in _LIVE_SLOT_IDS}
    last_source = ""
    while not _STOP:
        try:
            st = _http_json(f"{GW_BASE}/api/status", timeout=1.0)
            gfx = _http_json(f"{GW_BASE}/api/graphics/state", timeout=1.0)
            live = _http_json(f"{GW_BASE}/api/live/slots", timeout=1.0)
            pvw = _http_json(f"{GW_BASE}/api/pvw", timeout=1.0)
        except Exception as e:
            with _LOCK:
                _STATE.last_error = f"gateway poll error: {e}"
            time.sleep(0.5)
            continue

        # overlay updates (no restart)
        bug = (gfx.get("bug") or {})
        l3 = (gfx.get("lower_third") or {})
        _atomic_write_text(BUG_TEXT_PATH, bug.get("text") or "")
        _atomic_write_text(L3_TEXT_PATH, l3.get("text") or "")

        # determine live on-air slot + urls (we keep a per-slot decoder pool)
        slots = list(live.get("slots") or [])
        on_air = None
        for s in slots:
            if s.get("is_on_air"):
                on_air = s
                break
        live_id = (on_air or {}).get("slot_id") or None

        # determine automation item path
        current = (st.get("current") or {})
        item_path = str(current.get("path") or "")
        nxt = (st.get("next") or {})
        next_item_path = str(nxt.get("path") or "")
        nxt2 = (st.get("next2") or {})
        next2_item_path = str(nxt2.get("path") or "")

        
        # PVW selection (operator preview)
        desired_pvw_mode = "live"
        desired_pvw_slot = "cam_1"
        try:
            pd = (pvw.get("pvw") or {}) if isinstance(pvw, dict) else {}
            desired_pvw_mode = str(pd.get("mode") or "live")
            desired_pvw_slot = str(pd.get("slot") or "cam_1")
        except Exception:
            pass
        if desired_pvw_mode not in ("live", "next_auto", "program"):
            desired_pvw_mode = "live"

# source selection
        program = (st.get("program") or {})
        desired_source = str(program.get("source") or "automation")
        if desired_source not in ("automation", "live"):
            desired_source = "automation"

        # update state + switching telemetry
        with _LOCK:
            if desired_source != _STATE.selected_source:
                _STATE.switch_count += 1
                _STATE.last_switch_ts = time.time()
            _STATE.selected_source = desired_source
            _STATE.selected_live_slot = live_id
            _STATE.automation_path = item_path
            _STATE.next_automation_path = next_item_path
            _STATE.pvw_mode = desired_pvw_mode
            _STATE.pvw_live_slot = desired_pvw_slot

        # start/refresh decoders
        if _MEDIA_OK:            # automation decoder pool: promote next/next2 without restart when rundown advances
            if item_path:
                # If playlist advanced and current == previously warmed next, swap decoders (no restart)
                if item_path == last_next and _DEC_NEXT.input_desc == last_next:
                    _DEC_AUT, _DEC_NEXT, _DEC_NEXT2 = _DEC_NEXT, _DEC_NEXT2, _DEC_AUT
                    _DEC_NEXT2.stop()
                    last_item = item_path
                    last_next = next_item_path
                    last_next2 = next2_item_path
                # If we skipped and current == next2, promote next2 to current
                elif item_path == last_next2 and _DEC_NEXT2.input_desc == last_next2:
                    _DEC_AUT, _DEC_NEXT, _DEC_NEXT2 = _DEC_NEXT2, _DEC_AUT, _DEC_NEXT
                    _DEC_NEXT.stop()
                    _DEC_NEXT2.stop()
                    last_item = item_path
                    last_next = next_item_path
                    last_next2 = next2_item_path

            # ensure current automation decoder matches item_path
            if item_path and item_path != _DEC_AUT.input_desc:
                last_item = item_path
                try:
                    _DEC_AUT.start(FFMPEG, item_path, VIDEO_W, VIDEO_H, FPS)
                    with _LOCK:
                        _STATE.dec_aut_restarts = _DEC_AUT.restart_count
                except Exception as e:
                    with _LOCK:
                        _STATE.last_error = f"automation decoder error: {e}"

            # warm next clip decoder (PVW next)
            if next_item_path and next_item_path not in (item_path, "") and next_item_path != _DEC_NEXT.input_desc:
                last_next = next_item_path
                try:
                    _DEC_NEXT.start(FFMPEG, next_item_path, VIDEO_W, VIDEO_H, FPS)
                    with _LOCK:
                        _STATE.dec_next_restarts = _DEC_NEXT.restart_count
                except Exception as e:
                    with _LOCK:
                        _STATE.last_error = f"next decoder error: {e}"

            # warm next2 clip decoder (PVW next+1)
            if next2_item_path and next2_item_path not in (item_path, next_item_path, "") and next2_item_path != _DEC_NEXT2.input_desc:
                last_next2 = next2_item_path
                try:
                    _DEC_NEXT2.start(FFMPEG, next2_item_path, VIDEO_W, VIDEO_H, FPS)
                    with _LOCK:
                        _STATE.dec_next2_restarts = _DEC_NEXT2.restart_count
                except Exception as e:
                    with _LOCK:
                        _STATE.last_error = f"next2 decoder error: {e}"

            # live decoder pool: (re)start only the slot decoder whose URL changed.
            total_restarts = 0
            for sid in _LIVE_SLOT_IDS:
                dec = _DEC_LIVE_SLOTS.get(sid)
                if not dec:
                    continue
                # find the slot entry
                slot = next((x for x in slots if str(x.get("slot_id")) == sid), None)
                url = (slot or {}).get("url") or ""
                prev = last_live_urls.get(sid, "")
                if url and url != prev:
                    last_live_urls[sid] = url
                    try:
                        dec.start(FFMPEG, url, VIDEO_W, VIDEO_H, FPS)
                    except Exception as e:
                        with _LOCK:
                            _STATE.last_error = f"live decoder error ({sid}): {e}"
                total_restarts += dec.restart_count
            with _LOCK:
                _STATE.dec_live_restarts_total = total_restarts

        time.sleep(0.4)


def _compositor_loop() -> None:
    if not _MEDIA_OK:
        # no-op; legacy mode should be used in old versions
        return
    global _ENCODER_PROC
    last_preview_save = 0.0
    last_next_save = 0.0

    # ensure encoder running
    if _ENCODER_PROC is None or _ENCODER_PROC.poll() is not None:
        _start_encoder()

    blank = Image.new("RGB", (VIDEO_W, VIDEO_H), (0, 0, 0))  # type: ignore[attr-defined]
    last_good = blank
    last_good_ts = 0.0

    while not _STOP:
        p = _ENCODER_PROC
        if p is None or p.poll() is not None or p.stdin is None:
            # encoder died; restart
            _start_encoder()
            time.sleep(0.1)
            continue

        # select source frame
        with _LOCK:
            sel = _STATE.selected_source
            live_sid = _STATE.selected_live_slot

        jpeg = None
        if sel == "live" and live_sid:
            dec = _DEC_LIVE_SLOTS.get(str(live_sid))
            if dec and dec.reader.last_jpeg is not None:
                jpeg = dec.reader.last_jpeg
        if jpeg is None and _DEC_AUT.reader.last_jpeg is not None:
            jpeg = _DEC_AUT.reader.last_jpeg
        if jpeg is None:
            img = last_good
        else:
            try:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")  # type: ignore[attr-defined]
                if img.size != (VIDEO_W, VIDEO_H):
                    img = img.resize((VIDEO_W, VIDEO_H))
                last_good = img
                last_good_ts = time.time()
            except Exception:
                img = last_good

        # write to encoder stdin (rgb24)
        try:
            raw = img.tobytes()
            p.stdin.write(raw)
            with _LOCK:
                _STATE.last_frame_ts = time.time()
        except Exception as e:
            with _LOCK:
                _STATE.last_error = f"encoder write failed: {e}"
            _stop_encoder()
            time.sleep(0.2)
            continue

        # program preview jpeg save at ~5fps
        now = time.time()
        if now - last_preview_save > 0.2:
            last_preview_save = now
            try:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                _atomic_write_bytes(PREVIEW_PATH, buf.getvalue())
            except Exception:
                pass

        
        # PVW preview jpeg save at ~3fps (operator preview bus)
        if now - float(getattr(_STATE, "last_pvw_ts", 0.0)) > 0.33:
            try:
                with _LOCK:
                    pmode = _STATE.pvw_mode
                    pslot = _STATE.pvw_live_slot
                pimg = img
                if pmode == "next_auto":
                    pj = _DEC_NEXT.reader.last_jpeg
                    if pj and len(pj) > 1024:
                        pimg = Image.open(io.BytesIO(pj)).convert("RGB")  # type: ignore[attr-defined]
                        if pimg.size != (VIDEO_W, VIDEO_H):
                            pimg = pimg.resize((VIDEO_W, VIDEO_H))
                elif pmode == "live":
                    dec = _DEC_LIVE_SLOTS.get(str(pslot))
                    pj = dec.reader.last_jpeg if dec else None
                    if pj and len(pj) > 1024:
                        pimg = Image.open(io.BytesIO(pj)).convert("RGB")  # type: ignore[attr-defined]
                        if pimg.size != (VIDEO_W, VIDEO_H):
                            pimg = pimg.resize((VIDEO_W, VIDEO_H))
                # else program uses current img
                bufp = io.BytesIO()
                pimg.save(bufp, format="JPEG", quality=80)
                _atomic_write_bytes(PREVIEW_PVW_PATH, bufp.getvalue())
                with _LOCK:
                    _STATE.last_pvw_ts = now
            except Exception:
                pass

# PVW next-automation still from warm decoder (no extra ffmpeg)
        if now - last_next_save > 0.6:
            try:
                jpeg = _DEC_NEXT.reader.last_jpeg
                if jpeg and len(jpeg) > 1024:
                    _atomic_write_bytes(AUTO_NEXT_PREVIEW_PATH, jpeg)
                    last_next_save = now
            except Exception:
                pass

        # pace to FPS
        time.sleep(1.0 / max(1, FPS))



@app.get("/preview/pvw.jpg")
def preview_pvw_jpg():
    try:
        if PREVIEW_PVW_PATH.exists():
            return Response(content=PREVIEW_PVW_PATH.read_bytes(), media_type="image/jpeg")
    except Exception:
        pass
    raise HTTPException(404, "PVW preview not available")


@app.get("/health")
def health():
    return {"status": "ok", "service": "program_renderer", "media_ok": _MEDIA_OK}


@app.get("/status")
def status():
    with _LOCK:
        live_slot_status = {}
        for sid in _LIVE_SLOT_IDS:
            dec = _DEC_LIVE_SLOTS.get(sid)
            if not dec:
                continue
            live_slot_status[sid] = {
                "input": dec.input_desc,
                "restart_count": dec.restart_count,
                "last_ts": dec.reader.last_ts,
                "last_error": dec.last_error,
                "pid": (dec.proc.pid if dec.proc else None),
            }
        d = {
            "ok": True,
            "running": _STATE.running,
            "mode": _STATE.mode,
            "encoder_pid": _STATE.encoder_pid,
            "encoder_restarts": _STATE.encoder_restarts,
            "last_frame_ts": _STATE.last_frame_ts,
            "selected_source": _STATE.selected_source,
            "selected_live_slot": _STATE.selected_live_slot,
            "pvw_mode": _STATE.pvw_mode,
            "pvw_live_slot": _STATE.pvw_live_slot,
            "automation_path": _STATE.automation_path,
            "last_error": _STATE.last_error,
            "preview_path": str(PREVIEW_PATH),
            "auto_next_preview_path": str(AUTO_NEXT_PREVIEW_PATH),
            "next_automation_path": _STATE.next_automation_path,
            "switch_count": _STATE.switch_count,
            "last_switch_ts": _STATE.last_switch_ts,
            "dec_aut_restarts": _STATE.dec_aut_restarts,
            "dec_next_restarts": _STATE.dec_next_restarts,
            "dec_next2_restarts": _STATE.dec_next2_restarts,
            "dec_live_restarts_total": _STATE.dec_live_restarts_total,
            "dec_aut_last_ts": _DEC_AUT.reader.last_ts,
            "dec_next_last_ts": _DEC_NEXT.reader.last_ts,
            "dec_next2_last_ts": _DEC_NEXT2.reader.last_ts,
            "live_decoders": live_slot_status,
        }
    return JSONResponse(d)



@app.get("/preview/automation_next2.jpg")
def preview_automation_next2():
    # Warm-decoded N+2 still image (no extra ffmpeg one-shot needed)
    if _DEC_NEXT2.reader.last_jpeg:
        return Response(content=_DEC_NEXT2.reader.last_jpeg, media_type="image/jpeg")
    if _DEC_NEXT.reader.last_jpeg:
        return Response(content=_DEC_NEXT.reader.last_jpeg, media_type="image/jpeg")
    if AUTO_NEXT2_PREVIEW_PATH.exists():
        return Response(content=AUTO_NEXT2_PREVIEW_PATH.read_bytes(), media_type="image/jpeg")
    if AUTO_NEXT_PREVIEW_PATH.exists():
        return Response(content=AUTO_NEXT_PREVIEW_PATH.read_bytes(), media_type="image/jpeg")
    if PREVIEW_PATH.exists():
        return Response(content=PREVIEW_PATH.read_bytes(), media_type="image/jpeg")
    return Response(content=b"", media_type="image/jpeg")


@app.get("/preview/automation_next.jpg")
def preview_automation_next():
    # Warm-decoded next-clip still image for PVW when LIVE is on-air
    if _DEC_NEXT.reader.last_jpeg:
        return Response(content=_DEC_NEXT.reader.last_jpeg, media_type="image/jpeg")
    if AUTO_NEXT_PREVIEW_PATH.exists():
        return Response(content=AUTO_NEXT_PREVIEW_PATH.read_bytes(), media_type="image/jpeg")
    if PREVIEW_PATH.exists():
        return Response(content=PREVIEW_PATH.read_bytes(), media_type="image/jpeg")
    return Response(content=b"", media_type="image/jpeg")


@app.get("/preview/program.jpg")
def preview_program():
    if not PREVIEW_PATH.exists():
        return Response(content=b"", media_type="image/jpeg", status_code=404)
    return Response(content=PREVIEW_PATH.read_bytes(), media_type="image/jpeg")


@app.post("/control/restart")
def control_restart():
    _stop_encoder()
    if _MEDIA_OK:
        _start_encoder()
    return {"ok": True}



def _render_next_preview(path: str) -> None:
    """Fallback generator for next-automation preview.

    v4.11 prefers the warm `_DEC_NEXT` JPEG stream (no extra ffmpeg). This function
    remains as a best-effort fallback when `_DEC_NEXT` is not running/ready.
    """
    if not path or not os.path.exists(path):
        return
    tmp = AUTO_NEXT_PREVIEW_PATH.with_suffix(".tmp.jpg")
    cmd = [
        FFMPEG, "-y",
        "-ss", "0.5",
        "-i", path,
        "-frames:v", "1",
        "-vf", f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:black",
        "-q:v", "4",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.5, check=False)
        if tmp.exists() and tmp.stat().st_size > 1024:
            os.replace(str(tmp), str(AUTO_NEXT_PREVIEW_PATH))
        else:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

def _boot() -> None:
    global _POLL_THREAD, _COMPOSITOR_THREAD
    if _POLL_THREAD is None:
        _POLL_THREAD = threading.Thread(target=_poll_gateway_loop, daemon=True)
        _POLL_THREAD.start()
    if _COMPOSITOR_THREAD is None and _MEDIA_OK:
        _COMPOSITOR_THREAD = threading.Thread(target=_compositor_loop, daemon=True)
        _COMPOSITOR_THREAD.start()


_boot()