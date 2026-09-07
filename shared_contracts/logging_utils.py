
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("service", "correlation_id", "event_type", "request_id"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_json_logging(service: str, logs_root: str, level: str = "INFO") -> logging.Logger:
    """Create a JSONL daily-rotating logger for a service."""
    Path(logs_root).mkdir(parents=True, exist_ok=True)
    log_path = Path(logs_root) / service / f"{service}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"redtv.{service}")
    logger.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    logger.propagate = False

    if not any(isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in logger.handlers):
        h = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
            utc=False,
        )
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)

        sh = logging.StreamHandler()
        sh.setFormatter(JsonFormatter())
        logger.addHandler(sh)

    # attach service on record by default via LoggerAdapter
    return logging.LoggerAdapter(logger, {"service": service})  # type: ignore[return-value]
