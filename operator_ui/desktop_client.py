"""
operator_ui.desktop_client  (v4.16.2)
=====================================
PyQt6 desktop operator client for RED TV MCRX Master Control v4.16.2.

Features
--------
- Status panel: state badge, current/next clip, AI confidence, last error
- Control buttons: Play, Stop, Emergency, Reload Playlist
- WebSocket event log (auto-reconnect, colour-coded severity)
- Connection indicator with retry counter
- Keyboard shortcut: F12 → Emergency

Optional dependency — system works headlessly without PyQt6.
Run with:
    python -m operator_ui.desktop_client [--api http://localhost:8000]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading
from typing import Optional


def _check_pyqt6() -> bool:
    try:
        import PyQt6.QtWidgets  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Non-GUI: graceful error if PyQt6 not installed
# ---------------------------------------------------------------------------

if not _check_pyqt6():
    def main():
        print(
            "PyQt6 is not installed.  Install with:\n"
            "    pip install PyQt6 PyQt6-WebSockets\n"
            "The headless API gateway still works without it."
        )
        sys.exit(1)
else:
    # ── Only import Qt when available ──────────────────────────────────────
    from PyQt6.QtCore    import Qt, QTimer, pyqtSignal, QObject
    from PyQt6.QtGui     import QColor, QFont, QKeySequence, QShortcut, QPixmap
    from PyQt6.QtWidgets import (
        QApplication, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
        QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
        QPushButton, QSizePolicy, QSplitter, QStatusBar, QVBoxLayout,
        QWidget,
    )
    import urllib.request
    import urllib.error

    try:
        from PyQt6.QtWebSockets import QWebSocket
        from PyQt6.QtCore import QUrl
        _HAS_WS = True
    except ImportError:
        _HAS_WS = False


    class _WSWorker(QObject):
        """
        WebSocket worker in a separate thread.
        Emits Qt signals for main-thread UI updates (thread-safe).
        """
        event_received = pyqtSignal(str)
        connected      = pyqtSignal()
        disconnected   = pyqtSignal()

        def __init__(self, api_base: str) -> None:
            super().__init__()
            self._api_base    = api_base
            self._ws:         Optional["QWebSocket"] = None
            self._retry_count = 0
            self._running     = True

        def start(self) -> None:
            if not _HAS_WS:
                return
            self._connect()

        def stop(self) -> None:
            self._running = False
            if self._ws:
                self._ws.close()

        def _connect(self) -> None:
            if not _HAS_WS or not self._running:
                return
            ws_url = self._api_base.replace("http://", "ws://") \
                                    .replace("https://", "wss://") + "/events"
            self._ws = QWebSocket()
            self._ws.connected.connect(self._on_connected)
            self._ws.disconnected.connect(self._on_disconnected)
            self._ws.textMessageReceived.connect(self._on_message)
            self._ws.open(QUrl(ws_url))

        def _on_connected(self) -> None:
            self._retry_count = 0
            self.connected.emit()

        def _on_disconnected(self) -> None:
            self.disconnected.emit()
            if self._running:
                self._retry_count += 1
                delay = min(30, 2 ** min(self._retry_count, 5)) * 1000
                QTimer.singleShot(int(delay), self._connect)

        def _on_message(self, text: str) -> None:
            if text != '{"type":"ping"}':
                self.event_received.emit(text)


    class _StateBadge(QLabel):
        """Colour-coded state display."""

        STATE_COLOURS = {
            "PLAYING":    "#2dc653",
            "PRE_ROLL":   "#27ae60",
            "LOADING":    "#2980b9",
            "HOLD_FRAME": "#f4d03f",
            "EMERGENCY":  "#e63946",
            "ERROR":      "#c0392b",
            "IDLE":       "#555555",
            "SHUTDOWN":   "#333333",
        }

        def __init__(self) -> None:
            super().__init__("IDLE")
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setFont(QFont("Courier New", 20, QFont.Weight.Bold))
            self.setMinimumHeight(50)
            self._set_state("IDLE")

        def _set_state(self, state: str) -> None:
            colour = self.STATE_COLOURS.get(state, "#555555")
            self.setStyleSheet(
                f"background:{colour};color:#fff;padding:6px 16px;"
                f"border-radius:4px;letter-spacing:3px;"
            )
            self.setText(state)

        def update_state(self, state: str) -> None:
            self._set_state(state)


    class _MetricRow(QWidget):
        """label: value pair."""
        def __init__(self, label: str) -> None:
            super().__init__()
            lo = QHBoxLayout(self)
            lo.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label)
            lbl.setStyleSheet("color:#888;font-size:11px;")
            lbl.setFixedWidth(180)
            self._val = QLabel("—")
            self._val.setStyleSheet("font-family:'Courier New';font-size:12px;")
            lo.addWidget(lbl)
            lo.addWidget(self._val)
            lo.addStretch()

        def set_value(self, v: str, warn: bool = False, error: bool = False) -> None:
            colour = "#e63946" if error else ("#f4d03f" if warn else "#e8e8e8")
            self._val.setStyleSheet(
                f"font-family:'Courier New';font-size:12px;color:{colour};"
            )
            self._val.setText(v)


    class MainWindow(QMainWindow):
        def __init__(self, api_base: str, api_key: str = "") -> None:
            super().__init__()
            self._api_base = api_base.rstrip("/")
            self._api_key = (api_key or "").strip()
            self._setTitle()
            self.resize(1100, 720)

            # Dark theme
            self.setStyleSheet("""
                QMainWindow, QWidget { background:#0d0d0d; color:#e8e8e8; }
                QGroupBox { border:1px solid #2a2a2a; border-radius:4px;
                            margin-top:8px; font-size:10px; color:#e63946; }
                QGroupBox::title { subcontrol-origin:margin; padding:0 4px; }
                QPushButton { background:#222; border:1px solid #333; color:#e8e8e8;
                              padding:6px 14px; font-family:'Courier New'; font-size:11px; }
                QPushButton:hover { background:#333; }
                QPushButton#emergencyBtn { border-color:#e63946; color:#e63946; }
                QPushButton#emergencyBtn:hover { background:#1a0000; }
                QPushButton#playBtn { border-color:#2dc653; color:#2dc653; }
                QLineEdit { background:#0a0a0a; border:1px solid #2a2a2a; color:#e8e8e8;
                            padding:4px; font-family:'Courier New'; font-size:11px; }
                QListWidget { background:#0a0a0a; border:1px solid #2a2a2a;
                              font-family:'Courier New'; font-size:11px; }
                QSplitter::handle { background:#2a2a2a; }
                QStatusBar { background:#1a0000; color:#e8e8e8; font-size:10px; }
            """)

            self._build_ui()
            self._build_shortcuts()
            self._start_ws()

            # Poll status every 3s
            self._poll_timer = QTimer()
            self._poll_timer.timeout.connect(self._poll_status)
            self._poll_timer.start(3000)
            self._poll_status()

            self._preview_timer = QTimer()
            self._preview_timer.timeout.connect(self._refresh_live_preview)
            self._preview_timer.start(1000)
            self._refresh_live_preview()

        def _setTitle(self) -> None:
            self.setWindowTitle(
                f"RED TV MCRX v4.16.2 — {self._api_base}"
            )

        def _build_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)
            root_lo = QVBoxLayout(central)
            root_lo.setSpacing(6)
            root_lo.setContentsMargins(8, 8, 8, 8)

            # ── Title bar ──────────────────────────────────────────────
            title_row = QHBoxLayout()
            t = QLabel("🔴  RED TV MCRX MASTER CONTROL  v4.16.2")
            t.setStyleSheet("color:#e63946;font-size:14px;letter-spacing:2px;"
                            "font-family:'Courier New';font-weight:bold;")
            self._conn_label = QLabel("⬤ Disconnected")
            self._conn_label.setStyleSheet("color:#555;font-size:11px;")
            title_row.addWidget(t)
            title_row.addStretch()
            title_row.addWidget(self._conn_label)
            root_lo.addLayout(title_row)

            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color:#2a0000;")
            root_lo.addWidget(sep)

            # ── Main split: left=status+controls / right=events ────────
            splitter = QSplitter(Qt.Orientation.Horizontal)
            root_lo.addWidget(splitter, stretch=1)

            # LEFT ──────────────────────────────────────────────────────
            left = QWidget()
            left_lo = QVBoxLayout(left)
            left_lo.setSpacing(6)

            # State badge
            state_grp = QGroupBox("ENGINE STATE")
            sg_lo = QVBoxLayout(state_grp)
            self._state_badge = _StateBadge()
            sg_lo.addWidget(self._state_badge)
            left_lo.addWidget(state_grp)

            # Metrics
            metrics_grp = QGroupBox("METRICS")
            mg_lo = QVBoxLayout(metrics_grp)
            self._m: dict[str, _MetricRow] = {}
            for key, label in [
                ("current_item",          "Current Clip"),
                ("next_item",             "Next Clip"),
                ("ai_confidence",         "AI Confidence"),
                ("timeline_seconds",      "Timeline"),
                ("frame_count",           "Frames Out"),
                ("hold_count",            "Hold Frames"),
                ("emergency_count",       "Emergencies"),
                ("frame_drop_count",      "Frame Drops"),
                ("guard_violation_count", "Guard Violations"),
                ("drift_avg_ms",          "Avg Drift (ms)"),
                ("drift_error_count",     "Drift Errors"),
                ("buffer_depth",          "Buffer Depth"),
            ]:
                row = _MetricRow(label)
                self._m[key] = row
                mg_lo.addWidget(row)
            left_lo.addWidget(metrics_grp)

            # Last error
            err_grp = QGroupBox("LAST WARNING / ERROR")
            eg_lo = QVBoxLayout(err_grp)
            self._last_error = QLabel("—")
            self._last_error.setWordWrap(True)
            self._last_error.setStyleSheet(
                "font-family:'Courier New';font-size:10px;color:#f4d03f;"
            )
            eg_lo.addWidget(self._last_error)
            left_lo.addWidget(err_grp)

            # Controls
            ctrl_grp = QGroupBox("OPERATOR CONTROLS")
            cg_lo = QVBoxLayout(ctrl_grp)

            btn_row = QHBoxLayout()
            self._play_btn  = QPushButton("▶  PLAY / RESUME")
            self._play_btn.setObjectName("playBtn")
            self._stop_btn  = QPushButton("■  STOP")
            self._emerg_btn = QPushButton("⚡  EMERGENCY  [F12]")
            self._emerg_btn.setObjectName("emergencyBtn")
            self._reload_btn = QPushButton("↻  RELOAD")
            for b in (self._play_btn, self._stop_btn, self._emerg_btn, self._reload_btn):
                btn_row.addWidget(b)
            cg_lo.addLayout(btn_row)

            # Playlist path
            path_row = QHBoxLayout()
            self._playlist_path = QLineEdit()
            self._playlist_path.setPlaceholderText(
                "today.json  — resolved inside configured playlist directory"
            )
            load_btn = QPushButton("Load Playlist File")
            path_row.addWidget(self._playlist_path, stretch=1)
            path_row.addWidget(load_btn)
            cg_lo.addLayout(path_row)
            left_lo.addWidget(ctrl_grp)

            # ── Program source selector ────────────────────────────────
            src_grp = QGroupBox("PROGRAM SOURCE")
            src_grp_lo = QVBoxLayout(src_grp)

            src_banner_row = QHBoxLayout()
            self._src_label = QLabel("● AUTOMATION")
            self._src_label.setStyleSheet(
                "color:#2dc653;font-size:14px;font-weight:bold;"
                "font-family:'Courier New';letter-spacing:2px;"
            )
            src_banner_row.addWidget(self._src_label)
            src_banner_row.addStretch()
            src_grp_lo.addLayout(src_banner_row)

            src_btn_row = QHBoxLayout()
            self._btn_src_auto = QPushButton("▶ ON AIR: AUTOMATION")
            self._btn_src_auto.setObjectName("playBtn")
            self._btn_src_live = QPushButton("▶ ON AIR: LIVE")
            self._btn_src_live.setStyleSheet(
                "border:1px solid #e63946;color:#e63946;"
                "padding:6px 14px;font-size:11px;"
            )
            src_btn_row.addWidget(self._btn_src_auto)
            src_btn_row.addWidget(self._btn_src_live)
            src_grp_lo.addLayout(src_btn_row)
            left_lo.addWidget(src_grp)

            self._btn_src_auto.clicked.connect(
                lambda: self._api_post("/api/control/program/source", {"source": "automation"})
            )
            self._btn_src_live.clicked.connect(
                lambda: self._api_post("/api/control/program/source", {"source": "live"})
            )

            # ── Live source panel ──────────────────────────────────────
            live_grp = QGroupBox("LIVE SOURCE")
            lg_lo = QVBoxLayout(live_grp)

            self._lm: dict[str, _MetricRow] = {}
            for key, label in [
                ("l_state",      "Live State"),
                ("l_mode",       "Mode"),
                ("l_input",      "Input"),
                ("l_ready",      "Ready"),
                ("l_buffer",     "Buffer"),
                ("l_last_error", "Last Error"),
            ]:
                row = _MetricRow(label)
                self._lm[key] = row
                lg_lo.addWidget(row)

            # Live file row
            live_file_row = QHBoxLayout()
            self._live_file_path = QLineEdit()
            self._live_file_path.setPlaceholderText(
                "\\\\NAS-SERVER\\CHANNEL\\live\\whatsapp.mp4"
            )
            self._btn_load_live = QPushButton("📁 Load Live File")
            self._btn_browse_live = QPushButton("Browse…")
            live_file_row.addWidget(self._live_file_path, stretch=1)
            live_file_row.addWidget(self._btn_browse_live)
            live_file_row.addWidget(self._btn_load_live)
            lg_lo.addLayout(live_file_row)

            # SRT row
            srt_row = QHBoxLayout()
            self._srt_url_input = QLineEdit()
            self._srt_url_input.setPlaceholderText("srt://4g-cam-ip:1234")
            self._srt_name_input = QLineEdit()
            self._srt_name_input.setPlaceholderText("cam_1")
            self._srt_name_input.setMaximumWidth(70)
            self._btn_srt_connect = QPushButton("📡 SRT Connect")
            self._btn_stop_live   = QPushButton("■ Stop Live")
            srt_row.addWidget(self._srt_url_input, stretch=1)
            srt_row.addWidget(self._srt_name_input)
            srt_row.addWidget(self._btn_srt_connect)
            srt_row.addWidget(self._btn_stop_live)
            lg_lo.addLayout(srt_row)


            # Live preview image (best-effort, served from api_gateway -> live_ingest)
            self._live_preview = QLabel("Live preview not available")
            self._live_preview.setMinimumHeight(200)
            self._live_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._live_preview.setStyleSheet(
                "border:1px solid #2a2a2a; border-radius:6px; background:#0f0f0f; color:#999;")
            lg_lo.addWidget(self._live_preview)

            left_lo.addWidget(live_grp)

            # Wire live buttons
            self._btn_load_live.clicked.connect(self._on_load_live_file)
            self._btn_browse_live.clicked.connect(self._on_browse_live_file)
            self._btn_srt_connect.clicked.connect(self._on_srt_connect)
            self._btn_stop_live.clicked.connect(self._on_stop_live)

            left_lo.addStretch()


            # Wire buttons
            self._play_btn.clicked.connect(self._on_play)
            self._stop_btn.clicked.connect(self._on_stop)
            self._emerg_btn.clicked.connect(self._on_emergency)
            self._reload_btn.clicked.connect(self._poll_status)
            load_btn.clicked.connect(self._on_load_playlist)

            splitter.addWidget(left)

            # RIGHT: event log ─────────────────────────────────────────
            right = QWidget()
            right_lo = QVBoxLayout(right)
            events_grp = QGroupBox("LIVE EVENT STREAM")
            eg2_lo = QVBoxLayout(events_grp)
            self._event_list = QListWidget()
            self._event_list.setUniformItemSizes(True)
            eg2_lo.addWidget(self._event_list)
            clear_btn = QPushButton("Clear Log")
            clear_btn.clicked.connect(self._event_list.clear)
            eg2_lo.addWidget(clear_btn)
            right_lo.addWidget(events_grp)
            splitter.addWidget(right)
            splitter.setSizes([500, 600])

            # Status bar
            sb = QStatusBar()
            self.setStatusBar(sb)
            self._status_bar = sb
            self._status_bar.showMessage(f"Connecting to {self._api_base}...")

        def _build_shortcuts(self) -> None:
            emerg_sc = QShortcut(QKeySequence("F12"), self)
            emerg_sc.activated.connect(self._on_emergency)

        def _start_ws(self) -> None:
            if not _HAS_WS:
                self._append_event(
                    "PyQt6-WebSockets not installed — live events unavailable",
                    severity="WARNING"
                )
                return
            self._ws_worker = _WSWorker(self._api_base)
            self._ws_worker.event_received.connect(self._on_ws_event)
            self._ws_worker.connected.connect(self._on_ws_connected)
            self._ws_worker.disconnected.connect(self._on_ws_disconnected)
            self._ws_worker.start()

        # ── HTTP helpers ───────────────────────────────────────────────

        def _api_get(self, path: str) -> Optional[dict]:
            try:
                with urllib.request.urlopen(
                    f"{self._api_base}{path}", timeout=5
                ) as r:
                    return json.loads(r.read())
            except Exception as exc:
                self._last_error.setText(str(exc))
                return None

        def _api_post(self, path: str, body: Optional[dict] = None) -> Optional[dict]:
            try:
                data = json.dumps(body).encode() if body else b""
                headers = {"Content-Type": "application/json"}
                if self._api_key:
                    headers["X-API-Key"] = self._api_key
                req  = urllib.request.Request(
                    f"{self._api_base}{path}",
                    data=data,
                    method="POST",
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read())
            except Exception as exc:
                self._last_error.setText(str(exc))
                self._append_event(f"API error: {exc}", severity="ERROR")
                return None

        # ── Status poll ────────────────────────────────────────────────

        
        def _refresh_live_preview(self) -> None:
            """Fetch latest live preview JPEG and show it (best-effort)."""
            if not hasattr(self, "_live_preview"):
                return
            try:
                import urllib.request
                url = f"{self._api_base}/api/live/preview.jpg?ts={int(time.time()*1000)}"
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    data = resp.read()
                pm = QPixmap()
                ok = pm.loadFromData(data)
                if ok:
                    self._live_preview.setPixmap(pm.scaled(
                        self._live_preview.width(),
                        self._live_preview.height(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    ))
                else:
                    self._live_preview.setText("Live preview not available")
            except Exception:
                self._live_preview.setText("Live preview not available")

        def _poll_status(self) -> None:
            data = self._api_get("/api/status")
            if not data:
                return
            state = data.get("state", "IDLE")
            self._state_badge.update_state(state)

            for key, row in self._m.items():
                val = data.get(key)
                if val is None:
                    continue
                gv   = key == "guard_violation_count" and int(val) > 0
                drop = key == "frame_drop_count" and int(val) > 0
                hold = key == "hold_count" and int(val) > 10
                row.set_value(str(val), warn=hold or drop, error=gv)

            self._m["ai_confidence"].set_value("see events")

            # ── v3.3: program source + live panel ─────────────────────
            src = data.get("program_source", "automation")
            if src == "live":
                self._src_label.setText("● LIVE")
                self._src_label.setStyleSheet(
                    "color:#e63946;font-size:14px;font-weight:bold;"
                    "font-family:'Courier New';letter-spacing:2px;"
                )
            else:
                self._src_label.setText("● AUTOMATION")
                self._src_label.setStyleSheet(
                    "color:#2dc653;font-size:14px;font-weight:bold;"
                    "font-family:'Courier New';letter-spacing:2px;"
                )

            live = data.get("live", {})
            self._lm["l_state"].set_value(live.get("state", "idle"))
            self._lm["l_mode"].set_value(live.get("mode", "idle"))
            self._lm["l_input"].set_value(live.get("input", "—") or "—")
            ready = live.get("ready", False)
            self._lm["l_ready"].set_value(
                "YES" if ready else "NO",
                warn=not ready, error=False
            )
            self._lm["l_buffer"].set_value(
                str(live.get("buffer_depth", "—")) + " frames"
                if live.get("buffer_depth") is not None else "—"
            )
            err = live.get("last_error", "")
            self._lm["l_last_error"].set_value(err or "—", error=bool(err))

            self._status_bar.showMessage(
                f"State: {state} | PGM: {src.upper()} | "
                f"Frames: {data.get('frame_count',0)} | "
                f"Drops: {data.get('frame_drop_count',0)} | "
                f"Violations: {data.get('guard_violation_count',0)}"
            )

        # ── Button handlers ────────────────────────────────────────────

        def _on_play(self) -> None:
            # Load the path if filled in, otherwise just a reload
            path = self._playlist_path.text().strip()
            if path:
                self._on_load_playlist()
            self._append_event("▶ PLAY requested", severity="INFO")

        def _on_stop(self) -> None:
            # POST to a hypothetical /api/stop; fallback to loading empty
            self._api_post("/api/playlist/stop")
            self._append_event("■ STOP requested", severity="WARNING")

        def _on_emergency(self) -> None:
            if self._api_post("/api/emergency"):
                self._append_event("⚡ EMERGENCY activated", severity="CRITICAL")

        def _on_load_playlist(self) -> None:
            path = self._playlist_path.text().strip()
            if not path:
                return
            from pathlib import PureWindowsPath
            filename = PureWindowsPath(path).name if "\\" in path else Path(path).name
            result = self._api_post(f"/api/playlist/load_file?path={filename}")
            if result:
                self._append_event(
                    f"Playlist loaded: {result.get('playlist_id','?')[:8]}"
                    f"  corr={result.get('correlation_id','?')}",
                    severity="INFO"
                )
            self._poll_status()

        def _on_load_live_file(self) -> None:
            path = self._live_file_path.text().strip()
            if not path:
                self._append_event("Live file path is empty", severity="WARNING")
                return
            result = self._api_post("/api/live/file", {"path": path})
            if result:
                if result.get("live_state") == "error":
                    self._append_event(
                        f"Live file error: {result.get('error','?')}", severity="ERROR"
                    )
                else:
                    self._append_event(
                        f"Live file staging: {path}", severity="INFO"
                    )
            self._poll_status()

        def _on_browse_live_file(self) -> None:
            """Open native file dialog and send UNC path to /live/file."""
            try:
                from PyQt6.QtWidgets import QFileDialog
                path, _ = QFileDialog.getOpenFileName(
                    self,
                    "Select Live File",
                    "",
                    "Video Files (*.mp4 *.mov *.mxf *.ts *.avi);;All Files (*)"
                )
                if path:
                    # Normalise to UNC-style backslash on Windows
                    import os
                    if os.name == "nt":
                        path = path.replace("/", "\\")
                    self._live_file_path.setText(path)
                    self._on_load_live_file()
            except Exception as exc:
                self._append_event(f"Browse error: {exc}", severity="ERROR")

        def _on_srt_connect(self) -> None:
            url  = self._srt_url_input.text().strip()
            name = self._srt_name_input.text().strip() or "cam_1"
            if not url:
                self._append_event("SRT URL is empty", severity="WARNING")
                return
            result = self._api_post("/api/live/srt/connect", {"name": name, "url": url})
            if result:
                self._append_event(f"SRT connecting: {name} @ {url}", severity="INFO")
            self._poll_status()

        def _on_stop_live(self) -> None:
            self._api_post("/api/live/stop")
            self._append_event("■ Live source stopped", severity="WARNING")
            self._poll_status()



        # ── v3.3: Live source handlers ─────────────────────────────────

        def _on_load_live_file(self) -> None:
            path = self._live_file_path.text().strip()
            if not path:
                self._append_event("Live file path is empty", severity="WARNING")
                return
            result = self._api_post("/api/live/file", {"path": path})
            if result:
                self._append_event(
                    f"Live file staging: {path}",
                    severity="INFO"
                )
            self._poll_status()

        def _on_browse_live_file(self) -> None:
            """Open file dialog and populate the live file path field."""
            try:
                from PyQt6.QtWidgets import QFileDialog
                path, _ = QFileDialog.getOpenFileName(
                    self,
                    "Select Live File",
                    "",
                    "Media Files (*.mp4 *.mov *.mxf *.ts *.avi *.mkv);;All Files (*)"
                )
                if path:
                    self._live_file_path.setText(path)
            except Exception as exc:
                self._append_event(f"Browse error: {exc}", severity="ERROR")

        def _on_srt_connect(self) -> None:
            url  = self._srt_url_input.text().strip()
            name = self._srt_name_input.text().strip() or "cam_1"
            if not url:
                self._append_event("SRT URL is empty", severity="WARNING")
                return
            result = self._api_post("/api/live/srt/connect", {"name": name, "url": url})
            if result:
                self._append_event(
                    f"SRT connecting: {name} @ {url}",
                    severity="INFO"
                )
            self._poll_status()

        def _on_stop_live(self) -> None:
            result = self._api_post("/api/live/stop")
            if result:
                self._append_event("Live source stopped", severity="WARNING")
            self._poll_status()

        # ── WebSocket event handler ────────────────────────────────────

        def _on_ws_event(self, text: str) -> None:
            try:
                evt = json.loads(text)
            except Exception:
                return
            sev     = evt.get("severity", "info").upper()
            state   = evt.get("state", "")
            warning = evt.get("warning", "")
            item    = evt.get("current_item", "")
            corr    = evt.get("extra", {}).get("correlation_id", "")

            if state:
                self._state_badge.update_state(state)

            # v3.3: react to source-change events immediately
            if "PROGRAM_SOURCE" in state:
                self._poll_status()
            if "LIVE" in state:
                self._poll_status()

            parts = [f"[{sev}]"]
            if state:
                parts.append(state)
            if item:
                parts.append(f"→ {item}")
            if warning:
                parts.append(f"⚠ {warning}")
                self._last_error.setText(warning)
            if corr:
                parts.append(f"corr={corr}")

            self._append_event(" ".join(parts), severity=sev)

        def _on_ws_connected(self) -> None:
            self._conn_label.setText("⬤ Connected")
            self._conn_label.setStyleSheet("color:#2dc653;font-size:11px;")
            self._append_event("WebSocket connected", severity="INFO")

        def _on_ws_disconnected(self) -> None:
            self._conn_label.setText("⬤ Reconnecting…")
            self._conn_label.setStyleSheet("color:#f4d03f;font-size:11px;")
            self._append_event("WebSocket disconnected — retrying…", severity="WARNING")

        # ── Append event to list ───────────────────────────────────────

        def _append_event(self, msg: str, severity: str = "INFO") -> None:
            from PyQt6.QtWidgets import QListWidgetItem
            import time
            ts   = time.strftime("%H:%M:%S")
            item = QListWidgetItem(f"{ts}  {msg}")
            colour_map = {
                "CRITICAL": "#e63946",
                "ERROR":    "#c0392b",
                "WARNING":  "#f4d03f",
                "INFO":     "#e8e8e8",
                "DEBUG":    "#666",
            }
            col = colour_map.get(severity, "#e8e8e8")
            item.setForeground(QColor(col))
            self._event_list.addItem(item)
            self._event_list.scrollToBottom()
            # Cap at 1000 items
            while self._event_list.count() > 1000:
                self._event_list.takeItem(0)


    def main() -> None:
        parser = argparse.ArgumentParser(description="RED TV MCRX v4.16.2 Operator Client")
        parser.add_argument("--api", default="http://localhost:8000",
                            help="API gateway base URL")
        parser.add_argument(
            "--api-key",
            default=os.environ.get("REDTV_API_KEY", ""),
            help="Control API key (prefer REDTV_API_KEY env var to avoid shell history)",
        )
        args = parser.parse_args()

        app = QApplication(sys.argv)
        app.setApplicationName("RED TV MCRX v4.16.2")
        win = MainWindow(api_base=args.api, api_key=args.api_key)
        win.show()
        sys.exit(app.exec())


if __name__ == "__main__":
    main()
