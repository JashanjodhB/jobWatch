"""Template environment, filters, and the per-request helpers routes share."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import Database
from ..notify.discord import humanize_age
from ..service import Service

__all__ = [
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "get_db",
    "get_service",
    "nav_counts",
    "parse_locations",
    "partial",
    "render",
    "templates",
]

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def parse_locations(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return [str(x) for x in value] if isinstance(value, list) else []


def _places(raw: Any, limit: int = 3) -> str:
    """Locations as a board line: 'San Francisco · Seattle · +4 more'."""
    locs = parse_locations(raw)
    if not locs:
        return ""
    shown = " · ".join(locs[:limit])
    if len(locs) > limit:
        shown += f" · +{len(locs) - limit} more"
    return shown


templates.env.filters["age"] = humanize_age
templates.env.filters["places"] = _places
templates.env.filters["locations"] = parse_locations
templates.env.globals["classification_class"] = lambda c: {
    "match": "confirmed",
    "review": "signal",
    "reject": "",
}.get(c, "")


def get_service(request: Request) -> Service:
    return request.app.state.service


def get_db(request: Request) -> Database:
    return request.app.state.service.db


def nav_counts(db: Database) -> dict[str, int]:
    """Badge numbers in the tab bar. One cheap query each."""
    return {
        "review": db.scalar(
            "SELECT COUNT(DISTINCT normalized_title) FROM jobs "
            "WHERE classification = 'review' "
            "AND normalized_title NOT IN (SELECT normalized_title FROM title_verdicts)",
            default=0,
        ),
        "outbox_failed": db.scalar(
            "SELECT COUNT(*) FROM outbox WHERE status = 'failed'", default=0
        ),
        "alarms": db.scalar(
            "SELECT COUNT(*) FROM sources s JOIN companies c ON c.slug = s.company_slug "
            "WHERE s.enabled = 1 AND c.enabled = 1 AND s.consecutive_failures >= 5",
            default=0,
        ),
    }


def render(
    request: Request, template: str, context: dict[str, Any] | None = None, **extra: Any
) -> HTMLResponse:
    service = get_service(request)
    ctx: dict[str, Any] = {
        "request": request,
        "nav": nav_counts(service.db),
        "settings": service.config.settings,
        "path": request.url.path,
    }
    ctx.update(context or {})
    ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx)


def partial(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    """Render an HTMX fragment: no nav, no chrome."""
    return templates.TemplateResponse(request, template, {"request": request, **ctx})
