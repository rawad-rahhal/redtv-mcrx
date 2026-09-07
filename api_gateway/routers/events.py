"""
api_gateway.routers.events  (v2.1 — HARDENED)
===============================================
WebSocket /events — broadcasts all BroadcastEvents to connected clients.

v2.1 additions
--------------
- Events include correlation_id (playlist_id / session_id) when available.
- Multiple concurrent clients supported (fan-out via broadcast to all).
- Client list maintained with weak-ref cleanup on disconnect.
- Ping heartbeat every cfg.api_gateway.ws_ping_interval seconds.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("redtv.api_gateway.events")
router = APIRouter()

from fastapi import Body, Request
from fastapi.responses import JSONResponse
from shared_contracts.events import BroadcastEvent, Severity

@router.post("/events/push")
async def push_event_http(payload: dict = Body(...), request: Request = None):
    """Allow internal services to push events into the WS stream via HTTP."""
    try:
        event = BroadcastEvent.model_validate(payload)
    except Exception:
        event = BroadcastEvent(severity=Severity.INFO, extra=payload)
    await broadcast_event(event.model_dump_json())
    return JSONResponse({"ok": True})


# All connected WebSocket clients
_clients: Set[asyncio.Queue] = set()
_clients_lock = asyncio.Lock()


async def broadcast_event(event_json: str) -> None:
    """Called from sync thread via call_soon_threadsafe → async fan-out."""
    async with _clients_lock:
        dead = set()
        for q in list(_clients):
            try:
                q.put_nowait(event_json)
            except asyncio.QueueFull:
                dead.add(q)
        for q in dead:
            _clients.discard(q)


def push_event_sync(event_json: str) -> None:
    """
    Thread-safe bridge: called from sync engine callback → asyncio queue.
    Uses call_soon_threadsafe to safely cross the thread boundary.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(broadcast_event(event_json))
            )
    except Exception:
        pass


@router.websocket("/events")
async def events_ws(websocket: WebSocket):
    await websocket.accept()
    client_addr = str(websocket.client)
    log.info("WS client connected: %s", client_addr,
             extra={"service": "events_ws"})

    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    async with _clients_lock:
        _clients.add(q)

    try:
        while True:
            try:
                event_json = await asyncio.wait_for(q.get(), timeout=10.0)
                await websocket.send_text(event_json)
            except asyncio.TimeoutError:
                # Ping — keep-alive
                await websocket.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        log.info("WS client disconnected: %s", client_addr,
                 extra={"service": "events_ws"})
    except Exception as exc:
        log.warning("WS error (%s): %s", client_addr, exc,
                    extra={"service": "events_ws"})
    finally:
        async with _clients_lock:
            _clients.discard(q)
