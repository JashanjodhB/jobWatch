"""Feed — the default view (§10).

Reverse-chronological arrivals. The leftmost column is **detection age**, not
posting date: source timestamps are unreliable and often absent, and how long
ago *we* saw it is the number that decides whether you still have a shot.

One row per `merge_key`, not per job row. A role carried by two sources is one
arrival, attributed to the lowest priority number — the company's own careers
page — exactly as the alert was.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...db import utcnow
from ..deps import get_db, partial, render

router = APIRouter()

PAGE_SIZE = 100

APP_STATUSES = ("none", "saved", "applied", "interview", "offer", "rejected")

# One row per merge_key: the priority-1 source wins attribution, and the group's
# earliest sighting is the detection time.
FEED_SQL = """
SELECT * FROM (
    SELECT j.dedup_key, j.merge_key, j.company_slug, j.title, j.url, j.locations,
           j.req_id, j.classification, j.class_source, j.category, j.app_status,
           j.normalized_title, j.alerted_at,
           c.display_name, c.tier,
           s.adapter,
           MIN(j.first_seen_at) OVER (PARTITION BY j.merge_key) AS detected_at,
           COUNT(*)             OVER (PARTITION BY j.merge_key) AS source_count,
           ROW_NUMBER()         OVER (PARTITION BY j.merge_key
                                      ORDER BY s.priority, s.id)  AS rn
    FROM jobs j
    JOIN companies c ON c.slug = j.company_slug
    JOIN sources   s ON s.id   = j.source_id
    WHERE {where}
)
WHERE rn = 1
ORDER BY detected_at DESC
LIMIT ? OFFSET ?
"""


def _build_where(params: dict) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []

    show = params.get("show") or "alerts"
    if show == "alerts":
        clauses.append("j.classification IN ('match','review')")
    elif show == "review":
        clauses.append("j.classification = 'review'")
    elif show == "backlog":
        clauses.append("j.class_source = 'seed'")
    elif show == "rejected":
        clauses.append("j.classification = 'reject' AND j.class_source != 'seed'")
    # 'all' adds nothing

    if params.get("company"):
        clauses.append("j.company_slug = ?")
        args.append(params["company"])
    if params.get("category"):
        clauses.append("j.category = ?")
        args.append(params["category"])
    if params.get("tier"):
        clauses.append("c.tier = ?")
        args.append(params["tier"])
    if params.get("app_status"):
        clauses.append("j.app_status = ?")
        args.append(params["app_status"])
    if params.get("days"):
        clauses.append("j.first_seen_at >= datetime('now', ?)")
        args.append(f"-{int(params['days'])} days")
    if params.get("q"):
        clauses.append("(j.title LIKE ? OR c.display_name LIKE ? OR j.merge_key LIKE ?)")
        needle = f"%{params['q']}%"
        args.extend([needle, needle, f"{params['q']}%"])

    return (" AND ".join(clauses) or "1=1"), args


@router.get("/", response_class=HTMLResponse)
async def feed(request: Request) -> HTMLResponse:
    db = get_db(request)
    qp = request.query_params
    params = {
        "show": qp.get("show", "alerts"),
        "company": qp.get("company", ""),
        "category": qp.get("category", ""),
        "tier": qp.get("tier", ""),
        "app_status": qp.get("app_status", ""),
        "days": qp.get("days", ""),
        "q": qp.get("q", ""),
    }
    page = max(0, int(qp.get("page", 0) or 0))

    where, args = _build_where(params)
    rows = db.query(
        FEED_SQL.format(where=where), [*args, PAGE_SIZE + 1, page * PAGE_SIZE]
    )
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    kept = urlencode({k: v for k, v in params.items() if v})
    context = {
        "rows": rows,
        "params": params,
        "page": page,
        "has_more": has_more,
        "page_query": f"{kept}&" if kept else "",
        "companies": db.query(
            "SELECT slug, display_name FROM companies ORDER BY display_name"
        ),
        "categories": [
            r["category"]
            for r in db.query(
                "SELECT DISTINCT category FROM jobs WHERE category IS NOT NULL "
                "AND category != '' ORDER BY category"
            )
        ],
        "app_statuses": APP_STATUSES,
        "totals": _totals(db),
    }

    if request.headers.get("HX-Request") and qp.get("rows_only"):
        return partial(request, "_feed_rows.html", **context)
    return render(request, "feed.html", context)


def _totals(db) -> dict:
    return {
        "alerts_today": db.scalar(
            "SELECT COUNT(DISTINCT merge_key) FROM jobs "
            "WHERE alerted_at >= datetime('now','-1 day')",
            default=0,
        ),
        "alerts_week": db.scalar(
            "SELECT COUNT(DISTINCT merge_key) FROM jobs "
            "WHERE alerted_at >= datetime('now','-7 days')",
            default=0,
        ),
        "tracked": db.scalar("SELECT COUNT(*) FROM jobs", default=0),
        "sources": db.scalar("SELECT COUNT(*) FROM sources WHERE enabled = 1", default=0),
        "applied": db.scalar(
            "SELECT COUNT(*) FROM jobs WHERE app_status NOT IN ('none','saved')", default=0
        ),
    }


@router.post("/jobs/{key}/status", response_class=HTMLResponse)
async def set_status(
    request: Request, key: str, status: Annotated[str, Form()]
) -> HTMLResponse:
    """Marking a job applied is one click (§10)."""
    if status not in APP_STATUSES:
        status = "none"
    db = get_db(request)

    row = db.one("SELECT merge_key FROM jobs WHERE dedup_key = ?", (key,))
    if row is None:
        return partial(request, "_app_status.html", row=None)

    # Apply to every row of the group: it is one job, however many sources saw it.
    db.execute(
        "UPDATE jobs SET app_status = ?, app_updated_at = ? WHERE merge_key = ?",
        (status, utcnow(), row["merge_key"]),
    )
    updated = db.one(
        "SELECT dedup_key, app_status FROM jobs WHERE dedup_key = ?", (key,)
    )
    return partial(
        request, "_app_status.html", row=updated, app_statuses=APP_STATUSES
    )
