"""
shared_contracts.config_validator  (v4.0)
==========================================
Startup config validation.
Warns on missing required keys and suspicious UNC paths.
Does NOT raise — logs warnings and returns a list of issues.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

log = logging.getLogger("redtv.config_validator")


def validate_config(cfg: dict | None = None) -> List[str]:
    """
    Validate config dict.  If cfg is None, loads from config/redtv.yaml.
    Returns list of warning strings (empty = all good).
    """
    if cfg is None:
        try:
            import yaml
            config_path = Path(__file__).resolve().parents[1] / "config" / "redtv.yaml"
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            return [f"Cannot load config: {e}"]

    issues: List[str] = []

    # Required top-level sections
    for section in ("channel", "paths", "playout", "api_gateway", "live"):
        if section not in cfg:
            issues.append(f"Missing config section: [{section}]")

    # Playout values
    playout = cfg.get("playout", {})
    if playout.get("frame_interval_ms", 40) < 33:
        issues.append("playout.frame_interval_ms < 33ms — below 30fps")
    if playout.get("drift_error_ms", 100) < playout.get("drift_warn_ms", 20):
        issues.append("playout.drift_error_ms < drift_warn_ms — illogical thresholds")

    # UNC path sanity (Windows only)
    if os.name == "nt":
        paths = cfg.get("paths", {})
        for key, val in paths.items():
            if val and isinstance(val, str) and val.startswith("//"):
                # UNC paths should start with \\ on Windows
                issues.append(
                    f"paths.{key} uses forward-slash UNC ({val!r}). "
                    "On Windows use backslash UNC (\\\\NAS-SERVER\\...)"
                )

    # Live config
    live = cfg.get("live", {})
    if live.get("refuse_switch_if_not_ready") is False:
        issues.append(
            "live.refuse_switch_if_not_ready=false — unsafe! "
            "Switching to unready live may cause black output."
        )

    # API port sanity
    port = cfg.get("api_gateway", {}).get("port", 8000)
    if port < 1024:
        issues.append(f"api_gateway.port={port} — ports below 1024 require root/admin")

    for issue in issues:
        log.warning("CONFIG: %s", issue, extra={"service": "config_validator"})

    return issues


if __name__ == "__main__":
    import sys
    issues = validate_config()
    if issues:
        print(f"Config warnings ({len(issues)}):")
        for i in issues:
            print(f"  ⚠ {i}")
        sys.exit(1)
    else:
        print("Config OK")
        sys.exit(0)
