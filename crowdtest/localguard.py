"""Keeps a local tool server local: only requests addressed to 127.0.0.1/localhost, and only from its own page.
Stops other websites open in the same browser from driving it (cross-site requests, DNS rebinding)."""
from __future__ import annotations

from urllib.parse import urlparse

from starlette.responses import PlainTextResponse

LOCAL = {"127.0.0.1", "localhost", "[::1]"}


def install(app) -> None:
    @app.middleware("http")
    async def _guard(request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOCAL:
            return PlainTextResponse("This tool only answers on 127.0.0.1.", status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and (not origin or urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}):
            return PlainTextResponse("Requests must come from this tool's own page.", status_code=403)
        return await call_next(request)
