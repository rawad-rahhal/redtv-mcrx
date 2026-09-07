"""
api_gateway.app  (v4.16.2)
========================
FastAPI application for RED TV MCRX Master Control v4.16.2.

v4 additions
------------
- Graphics router wired (/api/graphics/*)
- config exposed on app.state for routers to access
- version bump to 4.16.2
- /status extended with graphics + live slots
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_gateway.routers.control  import router as control_router
from api_gateway.routers.events   import router as events_router, push_event_sync
from api_gateway.routers.health   import router as health_router
from api_gateway.routers.live     import router as live_router
from api_gateway.routers.graphics import router as graphics_router
from api_gateway.routers.guard    import router as guard_router
from api_gateway.routers.renderer import router as renderer_router
from api_gateway.routers.debug    import router as debug_router

from playout_core.engine          import PlayoutEngine
from playout_core.output_adapters import PreviewWindowAdapter
from playout_core.program_router  import ProgramRouter
from playout_core.sources.automation_source import AutomationSourceAdapter
from playout_core.sources.live_source       import LiveSourceAdapter
from shared_contracts.events import BroadcastEvent

log = logging.getLogger("redtv.api_gateway")




def _deep_merge(a: dict, b: dict) -> dict:
    """Deep merge b into a (dicts only). Returns new dict."""
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out.get(k) or {}, v)
        else:
            out[k] = v
    return out


def _maybe_apply_preset(cfg: dict) -> dict:
    """Optionally apply a customer preset overlay YAML.

    Priority:
      1) env REDTV_PRESET=path/to/preset.yaml OR name.yaml under config/presets/
      2) cfg.presets.active (name without extension) under config/presets/
    """
    active = os.environ.get("REDTV_PRESET") or ((cfg.get("presets") or {}).get("active") or "")
    if not active:
        return cfg

    repo_root = Path(__file__).resolve().parents[1]
    preset_path = Path(active)
    if not preset_path.exists():
        # treat as name under config/presets
        if not str(active).lower().endswith(".yaml"):
            active_name = f"{active}.yaml"
        else:
            active_name = str(active)
        preset_path = repo_root / "config" / "presets" / active_name

    if not preset_path.exists():
        logging.getLogger("redtv.api_gateway").warning("Preset not found: %s", preset_path)
        return cfg

    try:
        with open(preset_path, "r", encoding="utf-8") as f:
            preset = yaml.safe_load(f) or {}
        merged = _deep_merge(cfg, preset)
        merged.setdefault("presets", {}).setdefault("active_path", str(preset_path))
        return merged
    except Exception as e:
        logging.getLogger("redtv.api_gateway").exception("Failed to load preset %s: %s", preset_path, e)
        return cfg


# --- config loading (after helper defs) ---
config_path = Path(__file__).resolve().parents[1] / 'config' / 'redtv.yaml'
with open(config_path, 'r', encoding='utf-8') as f:
    CFG = yaml.safe_load(f) or {}
CFG = _maybe_apply_preset(CFG)

def setup_logging(cfg: dict) -> None:
    log_dir = cfg.get("logging", {}).get("log_dir", "logs")
    Path(log_dir).mkdir(exist_ok=True)
    level   = getattr(logging, cfg.get("logging", {}).get("level", "INFO"))
    handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "api_gateway.log"),
        when="midnight", backupCount=30, encoding="utf-8"
    )
    fmt = logging.Formatter(
        '{"ts":"%(asctime)s","svc":"%(name)s","lvl":"%(levelname)s","msg":"%(message)s"}'
    )
    handler.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=[handler, logging.StreamHandler()])


setup_logging(CFG)


def create_app() -> FastAPI:
    gw_cfg = CFG.get("api_gateway", {})
    cors   = gw_cfg.get("cors_origins", ["*"])

    preview_adapter = PreviewWindowAdapter(target_width=640, target_height=360)

    def _event_cb(evt: BroadcastEvent) -> None:
        push_event_sync(evt.to_json_log())
        log.info(evt.to_json_log(), extra={"service": "event_bus"})

    engine = PlayoutEngine(
        config         = CFG,
        output_adapter = preview_adapter,
        event_callback = _event_cb,
    )

    # ── Wire ProgramRouter for dual-source ──────────────────────────
    automation_src = AutomationSourceAdapter(engine._decoder)
    live_src = LiveSourceAdapter(
        guard           = engine._guard,
        pre_roll_frames = CFG.get("playout", {}).get("pre_roll_frames", 25),
        ring            = engine._ring,
    )
    program_router = ProgramRouter(
        automation = automation_src,
        live       = live_src,
        ring       = engine._ring,
    )
    engine.attach_program_router(program_router)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        live_src.start()
        engine.start()
        log.info(
            '{"event":"startup","version":"4.16.2","msg":"RED TV MCRX v4.16.2 online"}',
            extra={"service": "api_gateway"},
        )
        try:
            yield
        finally:
            engine.stop()
            live_src.stop()
            log.info(
                '{"event":"shutdown","msg":"RED TV MCRX v4.16.2 shutdown complete"}',
                extra={"service": "api_gateway"},
            )

    app = FastAPI(
        title    = "RED TV MCRX Master Control v4.16.2",
        version  = "4.16.2",
        docs_url = "/docs",
        lifespan = lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(control_router)
    app.include_router(events_router)
    app.include_router(live_router)
    app.include_router(graphics_router)
    app.include_router(guard_router)
    app.include_router(renderer_router)
    app.include_router(debug_router)
    app.add_route("/dashboard", _dashboard_handler)

    # Store everything on app.state
    app.state.version         = "4.16.2"
    app.state.engine          = engine
    app.state.preview_adapter = preview_adapter
    app.state.program_router  = program_router
    app.state.event_callback  = _event_cb
    app.state.config          = CFG   # routers can read config
    app.state.guard_report    = None  # last Broadcast Guard report
    app.state.guard_ack       = {"last_ack_at": None, "acked_violation_count": 0}

    return app


async def _dashboard_handler(request):
    dashboard = Path(__file__).resolve().parents[1] / "operator_ui" / "index.html"
    if dashboard.exists():
        return HTMLResponse(content=dashboard.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


app = create_app()