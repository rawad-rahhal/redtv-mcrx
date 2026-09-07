"""api_gateway.routers.health — /health and /status endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "service": "redtv_api_gateway"}


@router.get("/status")
def status(engine=None):
    """Engine is injected via app state."""
    from fastapi import Request
    return {"note": "use /api/status for engine status"}
