
import sys
from pathlib import Path

# ensure repo root on path for test runner
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient
from api_gateway.app import app


def test_health_and_status_endpoints_exist():
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.get("/status").status_code == 200
    assert c.get("/api/status").status_code == 200


def test_debug_runtime_snapshot_shape():
    c = TestClient(app)
    r = c.get("/api/debug/runtime")
    assert r.status_code == 200
    data = r.json()
    assert "engine" in data
    assert "guard" in data
    assert "renderer" in data
    assert "live_ingest" in data
