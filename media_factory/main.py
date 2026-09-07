"""
media_factory.main
==================
Ingest service entry point.

Runs:
  - DropFolderWatcher  (watches //NAS-SERVER/CHANNEL/ingest/drop)
  - IngestPool         (thread pool of N workers)
  - Queue reader       (reads queue.jsonl, dispatches jobs to pool)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared_contracts.models import IngestJob, NormalizationState
from media_factory.watcher import DropFolderWatcher
from media_factory.ingest_worker import IngestWorker


def setup_logging(cfg: dict) -> None:
    log_dir = cfg.get("logging", {}).get("log_dir", "logs")
    Path(log_dir).mkdir(exist_ok=True)
    level   = getattr(logging, cfg.get("logging", {}).get("level", "INFO"))
    handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "media_factory.log"),
        when="midnight", backupCount=30, encoding="utf-8"
    )
    fmt = logging.Formatter(
        '{"ts":"%(asctime)s","svc":"%(name)s","lvl":"%(levelname)s","msg":"%(message)s"}'
    )
    handler.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=[handler, logging.StreamHandler()])


log = logging.getLogger("redtv.media_factory.main")


class IngestQueue:
    """Reads queue.jsonl and dispatches jobs to IngestWorker pool."""

    def __init__(self, cfg: dict) -> None:
        self._cfg        = cfg
        self._queue_path = cfg.get("paths", {}).get("ingest_queue", "queue.jsonl")
        self._workers    = cfg.get("media_factory", {}).get("workers", 2)
        self._pool       = ThreadPoolExecutor(max_workers=self._workers,
                                              thread_name_prefix="ingest")
        self._stop       = threading.Event()
        self._processed: set[str] = set()

    def start(self) -> None:
        t = threading.Thread(target=self._reader_loop, daemon=True, name="queue-reader")
        t.start()
        log.info("IngestQueue reader started", extra={"service": "media_factory"})

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            if os.path.exists(self._queue_path):
                self._drain()
            self._stop.wait(3.0)

    def _drain(self) -> None:
        try:
            with open(self._queue_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                job  = IngestJob(**data)
            except Exception as exc:
                log.error("Bad queue entry: %s", exc,
                          extra={"service": "media_factory"})
                continue
            if job.job_id in self._processed:
                continue
            if job.state in (NormalizationState.DONE, NormalizationState.QUARANTINE):
                self._processed.add(job.job_id)
                continue
            self._processed.add(job.job_id)
            log.info("Dispatching job %s", job.job_id[:8],
                     extra={"service": "media_factory"})
            self._pool.submit(self._run_job, job)

    def _run_job(self, job: IngestJob) -> None:
        retry_max = self._cfg.get("media_factory", {}).get("retry_transient", 3)
        delay     = self._cfg.get("media_factory", {}).get("retry_delay_s", 5)
        worker    = IngestWorker(self._cfg)
        for attempt in range(retry_max):
            result = worker.process(job)
            if result.state == NormalizationState.DONE:
                return
            if result.state == NormalizationState.QUARANTINE:
                return
            log.warning("Job %s attempt %d failed — retrying in %ds",
                        job.job_id[:8], attempt + 1, delay,
                        extra={"service": "media_factory"})
            time.sleep(delay)


def main() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "redtv.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    setup_logging(cfg)
    paths = cfg.get("paths", {})

    queue_obj = IngestQueue(cfg)
    watcher   = DropFolderWatcher(
        watch_dir  = paths.get("ingest_watch", "./ingest/drop"),
        queue_path = paths.get("ingest_queue", "./ingest/queue.jsonl"),
        target_dir = paths.get("ingest_done",  "./ingest/done"),
    )

    queue_obj.start()
    watcher.start()

    def _stop(signum, frame):
        queue_obj.stop()
        watcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("media_factory service running", extra={"service": "media_factory"})
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
