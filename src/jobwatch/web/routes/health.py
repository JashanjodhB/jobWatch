"""Health — per-source status, with drift alarms sorted to the top (§10, §12).

A broken source is the failure mode that costs the most and announces itself the
least: the process is up, the logs are quiet, and an endpoint that used to return
four hundred postings now returns zero. This screen exists to make that visible
within a day.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from ...health import detect_drift
from ...logging_setup import get_logger
from ..deps import FAILING_AFTER, FAILING_SOURCES_SQL, get_db, get_service, partial, render

router = APIRouter()
log = get_logger(__name__)

SOURCE_SQL = """
SELECT s.*, c.display_name, c.tier, c.enabled AS company_enabled,
       (SELECT COUNT(*) FROM jobs j WHERE j.source_id = s.id) AS job_count,
       (SELECT max_count FROM source_baseline b
         WHERE b.source_id = s.id ORDER BY b.day DESC LIMIT 1) AS latest_count,
       (SELECT AVG(max_count) FROM (
            SELECT max_count FROM source_baseline b2
             WHERE b2.source_id = s.id ORDER BY b2.day DESC LIMIT 7
        )) AS baseline
FROM sources s
JOIN companies c ON c.slug = s.company_slug
ORDER BY s.consecutive_failures DESC, s.company_slug, s.priority
"""


@router.get("/health", response_class=HTMLResponse)
async def health(request: Request) -> HTMLResponse:
    db = get_db(request)
    alarms = detect_drift(db)
    alarm_by_source: dict[int, list] = {}
    for alarm in alarms:
        alarm_by_source.setdefault(alarm.source_id, []).append(alarm)

    sources = db.query(SOURCE_SQL)
    # Anything with an alarm floats to the top; the rest keep their query order.
    sources = sorted(
        sources,
        key=lambda r: (
            0 if any(a.severity == "alarm" for a in alarm_by_source.get(r["id"], [])) else
            1 if alarm_by_source.get(r["id"]) else 2,
            r["company_slug"],
        ),
    )

    # Set by the redirect after "Disable failing", so the page says what it did.
    just_disabled = None
    if "disabled" in request.query_params:
        ids = _ids(request.query_params["disabled"].split(","))
        just_disabled = db.query(
            f"SELECT company_slug, adapter FROM sources WHERE id IN ({_marks(ids)}) "
            "ORDER BY company_slug, adapter",
            ids,
        )

    return render(
        request,
        "health.html",
        {
            "sources": sources,
            "alarms": alarms,
            "alarm_by_source": alarm_by_source,
            "poll_log": db.query(
                "SELECT p.*, s.company_slug, s.adapter FROM poll_log p "
                "JOIN sources s ON s.id = p.source_id ORDER BY p.id DESC LIMIT 60"
            ),
            "stats": _stats(db),
            "failing": db.query(FAILING_SOURCES_SQL, (FAILING_AFTER,)),
            "failing_after": FAILING_AFTER,
            "just_disabled": just_disabled,
        },
    )


@router.post("/health/disable-failing")
async def disable_failing(
    request: Request, source_id: Annotated[list[str] | None, Form()] = None
) -> Response:
    """Switch off the failing sources the page listed, in one go.

    Only the ids the page showed, and only those still failing now: one that
    began failing after the page loaded was never in the count you confirmed,
    and one that recovered meanwhile is no longer broken. Disabled, never
    deleted (§13.4); each comes back with its toggle on Companies.
    """
    db = get_db(request)
    ids = _ids(source_id or [])
    with db.tx() as conn:
        rows = conn.execute(
            f"{FAILING_SOURCES_SQL} AND s.id IN ({_marks(ids)})", (FAILING_AFTER, *ids)
        ).fetchall()
        conn.executemany(
            "UPDATE sources SET enabled = 0 WHERE id = ?", [(r["id"],) for r in rows]
        )
    log.info(
        "failing_sources_disabled",
        sources=[f"{r['company_slug']}/{r['adapter']}" for r in rows],
        failing_after=FAILING_AFTER,
    )
    # A full page load rather than a swap: the table, the stats and the
    # masthead count all change.
    disabled = ",".join(str(r["id"]) for r in rows)
    return Response(status_code=204, headers={"HX-Redirect": f"/health?disabled={disabled}"})


def _ids(values: list[str]) -> list[int]:
    return [int(v) for v in values if v.strip().isdigit()]


def _marks(ids: list[int]) -> str:
    """Placeholders for `IN (...)`. `IN (NULL)` matches nothing, so an empty
    list stays valid SQL."""
    return ",".join("?" * len(ids)) or "NULL"


def _stats(db) -> dict:
    return {
        "sources": db.scalar("SELECT COUNT(*) FROM sources WHERE enabled = 1", default=0),
        "failing": db.scalar(
            "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND consecutive_failures > 0",
            default=0,
        ),
        "on_fallback": db.scalar(
            "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND using_fallback = 1", default=0
        ),
        "unseeded": db.scalar(
            "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND seeded = 0", default=0
        ),
        "polls_24h": db.scalar(
            "SELECT COUNT(*) FROM poll_log WHERE at >= datetime('now','-1 day')", default=0
        ),
        "errors_24h": db.scalar(
            "SELECT COUNT(*) FROM poll_log WHERE outcome = 'error' "
            "AND at >= datetime('now','-1 day')",
            default=0,
        ),
    }


@router.post("/health/recheck", response_class=HTMLResponse)
async def recheck(request: Request) -> HTMLResponse:
    """Force a drift pass now rather than waiting for the daily one."""
    service = get_service(request)
    service.health.check_drift()
    return partial(
        request, "_toast.html", message="Drift check complete — alarms refreshed"
    )
