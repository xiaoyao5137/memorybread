"""Instance-scoped authentication for browser-originated loopback requests."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Optional


TOKEN_HEADER = "X-MemoryBread-Local-Token"


def configured_token() -> Optional[str]:
    token_file = os.getenv("MEMORY_BREAD_LOCAL_AUTH_TOKEN_FILE", "").strip()
    if not token_file:
        return None
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token


def browser_request_authorized(origin: Optional[str], supplied_token: Optional[str]) -> bool:
    """Require the instance token for browser requests; permit internal clients.

    Service-to-service clients do not set Origin. Development mode has no token
    file configured and remains backward compatible.
    """
    expected = configured_token()
    if expected is None or not origin:
        return True
    if not expected or not supplied_token:
        return False
    return hmac.compare_digest(expected, supplied_token)


def install_flask_guard(app) -> None:
    from flask import jsonify, request

    @app.before_request
    def _verify_instance_token():
        if request.method == "OPTIONS":
            return None
        if browser_request_authorized(request.headers.get("Origin"), request.headers.get(TOKEN_HEADER)):
            return None
        return jsonify({"error": "LOCAL_AUTH_REQUIRED"}), 401


class LocalAuthASGIMiddleware:
    """Pure ASGI guard that preserves disconnect and streaming semantics."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        if scope.get("method") != "OPTIONS" and not browser_request_authorized(
            headers.get("origin"), headers.get(TOKEN_HEADER.lower())
        ):
            from fastapi.responses import JSONResponse

            response = JSONResponse(status_code=401, content={"error": "LOCAL_AUTH_REQUIRED"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_fastapi_guard(app) -> None:
    app.add_middleware(LocalAuthASGIMiddleware)
