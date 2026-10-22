"""Template environment, filters, and the per-request helpers routes share."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import Database
from ..notify.discord import humanize_age
from ..service import Service

__all__ = [
    "FAILING_AFTER",
    "FAILING_SOURCES_SQL",
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "asset_version",
    "get_db",
    "get_service",
    "nav_counts",
    "parse_locations",
    "partial",
    "pending_review_count",
    "render",
    "templates",
]

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


class _AssetVersion:
    """A fingerprint of static/, for cache-busting asset URLs.

    Pages link `/static/app.css?v=<this>`, which the browser then keeps for a
    year without asking (`app.py`). Editing any static file changes every URL,
    so a stale copy is never served and an unchanged one is never re-fetched.
    Names, sizes and mtimes rather than contents, so an edit made while the
    service runs is live within a second, as a template edit is; re-walking
    the directory costs ~2ms on Windows, hence at most once a second.
    """

    def __init__(self) -> None:
        self._value = ""
        self._checked = float("-inf")

    def __call__(self) -> str:
        now = time.monotonic()
        if now - self._checked >= 1.0:
            digest = hashlib.sha256()
            for path in sorted(STATIC_DIR.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    name = path.relative_to(STATIC_DIR).as_posix()
                    digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
            self._value = digest.hexdigest()[:12]
            self._checked = now
        return self._value


asset_version = _AssetVersion()


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
templates.env.globals["asset_version"] = asset_version
templates.env.globals["classification_class"] = lambda c: {
    "match": "confirmed",
    "review": "signal",
    "reject": "",
}.get(c, "")


def get_service(request: Request) -> Service:
    return request.app.state.service


def get_db(request: Request) -> Database:
    return request.app.state.service.db


#: Consecutive failed polls before a source counts as failing: the masthead's
#: "failing" count, the Health tab badge, and what "Disable failing" turns off.
FAILING_AFTER = 5

# Failing sources that are still being polled: a disabled company's sources
# are not, whatever their counters say.
FAILING_SOURCES_SQL = """
SELECT s.id, s.company_slug, s.adapter, s.consecutive_failures
FROM sources s
JOIN companies c ON c.slug = s.company_slug
WHERE s.enabled = 1 AND c.enabled = 1 AND s.consecutive_failures >= ?
"""


def pending_review_count(db: Database) -> int:
    """Distinct titles still waiting in the review queue."""
    return db.scalar(
        "SELECT COUNT(DISTINCT normalized_title) FROM jobs "
        "WHERE classification = 'review' "
        "AND normalized_title NOT IN (SELECT normalized_title FROM title_verdicts)",
        default=0,
    )


def nav_counts(db: Database) -> dict[str, int]:
    """Badge numbers in the tab bar. One cheap query each."""
    return {
        "review": pending_review_count(db),
        "outbox_failed": db.scalar(
            "SELECT COUNT(*) FROM outbox WHERE status = 'failed'", default=0
        ),
        "alarms": db.scalar(
            f"SELECT COUNT(*) FROM ({FAILING_SOURCES_SQL})", (FAILING_AFTER,), default=0
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
