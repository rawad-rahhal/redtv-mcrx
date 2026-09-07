"""
media_factory.probe
===================
ffprobe wrapper — extracts video/audio metadata from any media file.
Returns a validated MediaMetadata object (minus the paths, which are
filled in by the ingest worker).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Optional

from shared_contracts.models import AudioSpec, MediaMetadata, NormalizationState, VideoSpec, ScanType, FieldOrder

log = logging.getLogger("redtv.media_factory.probe")


class ProbeError(RuntimeError):
    pass


def probe_file(path: str, ffprobe_binary: str = "ffprobe") -> dict:
    """Run ffprobe and return raw JSON dict."""
    cmd = [
        ffprobe_binary,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        raise ProbeError(f"ffprobe not found at: {ffprobe_binary}")
    except subprocess.TimeoutExpired:
        raise ProbeError(f"ffprobe timed out on: {path}")

    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed: {result.stderr[:500]}")

    return json.loads(result.stdout)


def build_metadata_from_probe(
    probe_data:    dict,
    original_path: str,
    title:         Optional[str] = None,
) -> MediaMetadata:
    """Convert ffprobe JSON to MediaMetadata.  Raises ProbeError on bad input."""
    streams = probe_data.get("streams", [])
    fmt     = probe_data.get("format", {})

    video_stream = next((s for s in streams if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in streams if s["codec_type"] == "audio"), None)

    if not video_stream:
        raise ProbeError(f"No video stream found in: {original_path}")

    # Duration
    duration = float(fmt.get("duration") or video_stream.get("duration", 0))
    if duration <= 0:
        raise ProbeError("Could not determine duration")

    # Frame rate
    r_frame_rate = video_stream.get("r_frame_rate", "25/1")
    avg_frame_rate = video_stream.get("avg_frame_rate", r_frame_rate)
    frame_rate_str = avg_frame_rate if avg_frame_rate != "0/0" else r_frame_rate

    # Scan type / field order
    field_order_raw = video_stream.get("field_order", "progressive")
    if field_order_raw in ("tt", "tb", "tff"):
        scan_type   = ScanType.INTERLACED
        field_order = FieldOrder.TFF
    elif field_order_raw in ("bb", "bt", "bff"):
        scan_type   = ScanType.INTERLACED
        field_order = FieldOrder.BFF
    else:
        scan_type   = ScanType.PROGRESSIVE
        field_order = FieldOrder.PROGRESSIVE

    # Bitrates
    v_bitrate = None
    if "bit_rate" in video_stream:
        try:
            v_bitrate = int(video_stream["bit_rate"]) // 1000
        except (ValueError, TypeError):
            pass

    video = VideoSpec(
        width       = int(video_stream.get("width", 1920)),
        height      = int(video_stream.get("height", 1080)),
        frame_rate  = frame_rate_str,
        scan_type   = scan_type,
        field_order = field_order,
        color_space = video_stream.get("color_space", "bt709"),
        pixel_format = video_stream.get("pix_fmt", "yuv422p"),
        bitrate_kbps = v_bitrate,
        codec       = video_stream.get("codec_name", "h264"),
    )

    # Audio
    if audio_stream:
        a_bitrate = 192
        try:
            a_bitrate = int(audio_stream.get("bit_rate", 192000)) // 1000
        except (ValueError, TypeError):
            pass
        audio = AudioSpec(
            codec       = audio_stream.get("codec_name", "aac"),
            sample_rate = int(audio_stream.get("sample_rate", 48000)),
            channels    = int(audio_stream.get("channels", 2)),
            bitrate_kbps = a_bitrate,
            layout      = audio_stream.get("channel_layout", "stereo"),
        )
    else:
        log.warning("No audio stream in %s — using default spec", original_path,
                    extra={"service": "probe"})
        audio = AudioSpec()

    import os
    filename = os.path.basename(original_path)
    return MediaMetadata(
        title              = title or os.path.splitext(filename)[0],
        original_filename  = filename,
        normalized_path    = "",     # filled in by ingest worker
        duration_seconds   = duration,
        video              = video,
        audio              = audio,
        norm_state         = NormalizationState.PENDING,
    )
