
"""scripts.support_dump
Create a support bundle zip: configs + last 3 days logs + runtime snapshot + versions.

Usage:
  python -m scripts.support_dump --base http://127.0.0.1:8000 --out support_bundle.zip
"""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from pathlib import Path
import urllib.request


def _get_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="api_gateway base url")
    ap.add_argument("--out", default="support_bundle.zip")
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cfg_dir = repo_root / "config"

    out_zip = Path(args.out).resolve()
    if out_zip.exists():
        out_zip.unlink()

    runtime = {}
    try:
        runtime = _get_json(args.base.rstrip("/") + "/api/debug/runtime")
    except Exception as e:
        runtime = {"error": str(e), "ts": time.time()}

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # configs
        for p in cfg_dir.rglob("*"):
            if p.is_file():
                z.write(p, arcname=str(Path("config") / p.relative_to(cfg_dir)))

        # runtime snapshot
        z.writestr("runtime_snapshot.json", json.dumps(runtime, ensure_ascii=False, indent=2))

        # versions
        z.writestr("versions.txt", f"generated_at={time.ctime()}\nbase={args.base}\n")

        # logs (best effort: include any logs folder found)
        logs_root = None
        try:
            redtv = (cfg_dir / "redtv.yaml").read_text(encoding="utf-8", errors="ignore")
            if "logs_root" in redtv:
                # naive parse
                import yaml
                cfg = yaml.safe_load(redtv) or {}
                logs_root = ((cfg.get("paths") or {}).get("logs_root") or "")
        except Exception:
            logs_root = None

        candidates = []
        if logs_root:
            candidates.append(Path(logs_root))
        candidates.append(repo_root / "logs")
        for cand in candidates:
            if cand.exists():
                for p in cand.rglob("*.jsonl"):
                    # last N days by mtime
                    age_days = (time.time() - p.stat().st_mtime) / 86400.0
                    if age_days <= args.days:
                        try:
                            z.write(p, arcname=str(Path("logs") / p.relative_to(cand)))
                        except Exception:
                            pass

    print(str(out_zip))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
