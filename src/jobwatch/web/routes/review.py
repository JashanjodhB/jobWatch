"""Review queue — the classification workhorse (§10).

One card at a time, keyboard-first: `J`/`K` move, `M` match, `R` reject, `1`–`4`
tag a category, `U` undo. Thirty items in under a minute without touching the
mouse is the bar, so a verdict writes and swaps in the next card in one request.

A verdict is permanent and global: it writes `title_verdicts` keyed by the
normalized title, so every future posting with that title anywhere classifies
instantly, forever, at zero cost. The queue empties itself — the space of
distinct internship titles at a fixed set of companies is small and repeats hard.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..deps import get_service, parse_locations, partial, render

router = APIRouter()

# The 1–4 keys. Four is deliberate: more would need a glance at the screen.
CATEGORIES = ("swe", "ai-ml", "data", "infra")

QUEUE_SQL = """
SELECT j.dedup_key, j.title, j.normalized_title, j.locations, j.url, j.req_id,
       j.company_slug, j.first_seen_at, c.display_name, c.tier,
       COUNT(*) AS occurrences
FROM jobs j
JOIN companies c ON c.slug = j.company_slug
WHERE j.classification = 'review'
  AND j.normalized_title NOT IN (SELECT normalized_title FROM title_verdicts)
GROUP BY j.normalized_title
ORDER BY MAX(j.first_seen_at) DESC
"""


def _queue(db) -> list:
    return db.query(QUEUE_SQL)


def _card_context(request: Request, offset: int, undo_title: str | None = None) -> dict:
    service = get_service(request)
    queue = _queue(service.db)
    total = len(queue)
    offset = max(0, min(offset, max(0, total - 1)))
    row = queue[offset] if queue else None

    explanation = None
    if row is not None:
        explanation = service.classifier.explain(
            row["title"], parse_locations(row["locations"])
        )

    return {
        "row": row,
        "offset": offset,
        "total": total,
        "remaining": max(0, total - offset),
        "explanation": explanation,
        "categories": CATEGORIES,
        "undo_title": undo_title,
    }


@router.get("/review", response_class=HTMLResponse)
async def review(request: Request) -> HTMLResponse:
    offset = int(request.query_params.get("offset", 0) or 0)
    return render(request, "review.html", _card_context(request, offset))


@router.get("/review/card", response_class=HTMLResponse)
async def review_card(request: Request) -> HTMLResponse:
    offset = int(request.query_params.get("offset", 0) or 0)
    undo = request.query_params.get("undo") or None
    return partial(request, "_review_card.html", **_card_context(request, offset, undo))


@router.post("/review/{key}/verdict", response_class=HTMLResponse)
async def record_verdict(
    request: Request,
    key: str,
    verdict: Annotated[str, Form()],
    category: Annotated[str, Form()] = "",
    offset: Annotated[int, Form()] = 0,
) -> HTMLResponse:
    """Record a human decision and hand back the next card."""
    service = get_service(request)
    db = service.db

    row = db.one(
        "SELECT normalized_title, title, company_slug FROM jobs WHERE dedup_key = ?", (key,)
    )
    if row is None or verdict not in ("match", "reject"):
        return partial(request, "_review_card.html", **_card_context(request, offset))

    normalized = row["normalized_title"]
    tag = category.strip() or None

    with db.tx():
        service.classifier.record(
            normalized,
            verdict,
            source="manual",
            category=tag,
            sample_title=row["title"],
            sample_company=row["company_slug"],
        )
        service.classifier.apply_verdict_to_jobs(normalized, verdict, tag)

    # Staying at the same offset lands on the next item, because the one just
    # decided has left the queue.
    return partial(
        request, "_review_card.html", **_card_context(request, offset, undo_title=normalized)
    )


@router.post("/review/undo", response_class=HTMLResponse)
async def undo(
    request: Request,
    normalized_title: Annotated[str, Form()],
    offset: Annotated[int, Form()] = 0,
) -> HTMLResponse:
    """Drop a verdict and put its postings back in the queue."""
    service = get_service(request)
    with service.db.tx() as conn:
        service.classifier.forget(normalized_title)
        conn.execute(
            "UPDATE jobs SET classification = 'review', class_source = 'rules', "
            "category = NULL WHERE normalized_title = ?",
            (normalized_title,),
        )
    return partial(request, "_review_card.html", **_card_context(request, offset))
