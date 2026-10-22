"""FastAPI app factory (§10).

Runs as an asyncio task beside the scheduler, sharing the event loop and the
SQLite connection manager. Every unhandled route error is caught here and turned
into a rendered 500 — combined with the supervision in `main.py`, that is what
makes §13.8 true: a web bug is an inconvenience, never an outage of the poller.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response
from starlette.types import Scope

from ..logging_setup import get_logger
from ..service import Service
from .deps import STATIC_DIR, templates
from .routes import companies, feed, filters, health, outbox, review

log = get_logger(__name__)


class _VersionedStatic(StaticFiles):
    """Static files, cacheable for a year when the URL carries `?v=`.

    Pages only ever link assets with the version from `deps.asset_version`,
    which changes whenever a file does, so a cached copy can never be stale.
    Without this every navigation re-validated the stylesheet and both
    scripts. Unversioned URLs (the fonts, linked from app.css) keep the
    default headers.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if versioned and response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def create_app(service: Service) -> FastAPI:
    app = FastAPI(
        title="jobwatch",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service

    # The registry and health pages are 0.5-1.7 MB of repetitive markup, read
    # over Tailscale from a phone; gzip takes the registry to 75 KB. Starlette
    # defaults to level 9, but 6 is within a few percent of its size for well
    # under its CPU — and that CPU is the poller's event loop.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    app.mount("/static", _VersionedStatic(directory=str(STATIC_DIR)), name="static")

    app.include_router(feed.router)
    app.include_router(review.router)
    app.include_router(filters.router)
    app.include_router(companies.router)
    app.include_router(health.router)
    app.include_router(outbox.router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> HTMLResponse:
        log.error(
            "web_route_error",
            path=str(request.url.path),
            error=f"{type(exc).__name__}: {exc}",
            exc_info=exc,
        )
        if request.headers.get("HX-Request"):
            return PlainTextResponse(
                f"{type(exc).__name__}: {exc}", status_code=500
            )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    @app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
    async def healthz() -> str:
        return "ok"

    return app
