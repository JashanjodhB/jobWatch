"""FastAPI app factory (§10).

Runs as an asyncio task beside the scheduler, sharing the event loop and the
SQLite connection manager. Every unhandled route error is caught here and turned
into a rendered 500 — combined with the supervision in `main.py`, that is what
makes §13.8 true: a web bug is an inconvenience, never an outage of the poller.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..logging_setup import get_logger
from ..service import Service
from .deps import STATIC_DIR, templates
from .routes import companies, feed, filters, health, outbox, review

log = get_logger(__name__)


def create_app(service: Service) -> FastAPI:
    app = FastAPI(
        title="jobwatch",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
