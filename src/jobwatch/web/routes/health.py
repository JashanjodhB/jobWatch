"""Health — per-source status, with drift alarms sorted to the top (§10, §12).

A broken source is the failure mode that costs the most and announces itself the
least: the process is up, the logs are quiet, and an endpoint that used to return
four hundred postings now returns zero. This screen exists to make that visible
within a day.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...health import detect_drift
from ..deps import get_db, get_service, partial, render

router = APIRouter()

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
        },
    )


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
