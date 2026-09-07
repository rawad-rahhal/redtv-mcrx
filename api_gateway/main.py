"""api_gateway.main — start the gateway with uvicorn."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

config_path = Path(__file__).resolve().parents[1] / "config" / "redtv.yaml"
with open(config_path) as f:
    CFG = yaml.safe_load(f)

if __name__ == "__main__":
    gw = CFG.get("api_gateway", {})
    uvicorn.run(
        "api_gateway.app:app",
        host    = gw.get("host", "0.0.0.0"),
        port    = gw.get("port", 8000),
        reload  = False,
        workers = 1,
        log_level = "info",
    )
