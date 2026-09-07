"""API-key boundary for mutating MCRX HTTP control operations.

The gateway intentionally keeps read-only status/preview endpoints available on a
trusted control network while requiring an operator/service key for state-changing
HTTP requests. The secret is supplied only through ``REDTV_API_KEY`` and is never
read from repository configuration.

This is an authentication boundary, not RBAC: every valid key currently has the
same control authority. Network isolation is still required for the directly
reachable ingest/renderer services until they receive their own service identity.
"""
from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_API_KEY_ENV = "REDTV_API_KEY"
_API_KEY_HEADER = "X-API-Key"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _configured_api_key() -> str:
    """Return the configured control key, or an empty string when absent."""
    return os.environ.get(_API_KEY_ENV, "").strip()


def _is_protected_request(scope: Scope) -> bool:
    if scope.get("type") != "http":
        return False
    method = str(scope.get("method", "GET")).upper()
    if method not in _MUTATING_METHODS:
        return False
    path = str(scope.get("path", ""))
    return path.startswith("/api/") or path == "/events/push"


class MutatingAPIKeyMiddleware:
    """Fail-closed API-key check for state-changing gateway HTTP requests.

    - GET/HEAD/OPTIONS and WebSocket traffic are unaffected.
    - If the server has no ``REDTV_API_KEY``, protected requests fail with 503.
    - Missing or incorrect ``X-API-Key`` values fail with 401.
    - Comparison is constant-time and the supplied key is never logged or echoed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_protected_request(scope):
            await self.app(scope, receive, send)
            return

        expected = _configured_api_key()
        if not expected:
            response = JSONResponse(
                status_code=503,
                content={"detail": "Control API authentication is not configured"},
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        supplied = Headers(scope=scope).get(_API_KEY_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, expected):
            response = JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
                headers={
                    "Cache-Control": "no-store",
                    "WWW-Authenticate": "APIKey",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
