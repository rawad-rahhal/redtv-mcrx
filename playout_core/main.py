"""
playout_core.main
=================
Standalone entry point for the playout_core service.
In production, this process is launched by the Windows service wrapper
or the run_all.bat script.

It:
  1. Loads config/redtv.yaml
  2. Instantiates PlayoutEngine with PreviewWindowAdapter
  3. Registers an event bus connection to the API gateway via a shared queue
  4. Blocks until SIGINT / SIGTERM
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

import yaml

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playout_core.engine import PlayoutEngine
from playout_core.output_adapters import PreviewWindowAdapter
from shared_contracts.events import BroadcastEvent


def setup_logging(cfg: dict) -> None:
    import logging.handlers
    log_dir  = cfg.get("logging", {}).get("log_dir", "logs") or "logs"
    Path(log_dir).mkdir(exist_ok=True)
    level    = getattr(logging, cfg.get("logging", {}).get("level", "INFO"))
    handler  = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "playout_core.log"),
        when="midnight", backupCount=30, encoding="utf-8"
    )
    fmt = logging.Formatter(
        '{"ts":"%(asctime)s","svc":"%(name)s","lvl":"%(levelname)s","msg":"%(message)s"}'
    )
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=[handler, console])


def main() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "redtv.yaml"
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    setup_logging(cfg)
    log = logging.getLogger("redtv.playout.main")

    adapter = PreviewWindowAdapter(target_width=640, target_height=360)

    def event_handler(evt: BroadcastEvent) -> None:
        log.info(evt.to_json_log(), extra={"service": "event_bus"})

    engine = PlayoutEngine(
        config         = cfg,
        output_adapter = adapter,
        event_callback = event_handler,
    )
    engine.start()

    def _shutdown(signum, frame):
        log.info("Signal %d received — shutting down", signum,
                 extra={"service": "playout_main"})
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Playout core running.  Press Ctrl+C to stop.",
             extra={"service": "playout_main"})

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
