"""Filter workbench — regex tuning as a diff, not guesswork (§10).

As you type a pattern the panel re-classifies the last 90 days of titles with
and without it, and shows exactly which postings would flip in each direction.
That is the difference between "this regex looks about right" and knowing it
would have suppressed four internships you actually wanted.

Saving any rule purges every `source='rules'` verdict. Without that, a workbench
edit would have no effect on titles the old rules had already cached — the
change would silently apply to nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...classify.rules import KINDS, RuleSet
from ...config import export_config
from ...db import utcnow
from ..deps import get_db, get_service, parse_locations, partial, render

router = APIRouter()

SAMPLE_DAYS = 90
MAX_FLIPS_SHOWN = 40


def _rules_by_kind(db) -> dict[str, list]:
    grouped: dict[str, list] = {k: [] for k in KINDS}
    for row in db.query("SELECT * FROM filter_rules ORDER BY kind, id"):
        grouped.setdefault(row["kind"], []).append(row)
    return grouped


def _sample_titles(db, days: int = SAMPLE_DAYS) -> list[tuple[str, list[str], str]]:
    rows = db.query(
        "SELECT title, locations, company_slug, MAX(first_seen_at) AS seen FROM jobs "
        "WHERE first_seen_at >= datetime('now', ?) "
        "GROUP BY normalized_title ORDER BY seen DESC",
        (f"-{days} days",),
    )
    return [(r["title"], parse_locations(r["locations"]), r["company_slug"]) for r in rows]


@router.get("/filters", response_class=HTMLResponse)
async def workbench(request: Request) -> HTMLResponse:
    db = get_db(request)
    ruleset = RuleSet.load(db, enabled_only=False)
    return render(
        request,
        "filters.html",
        {
            "grouped": _rules_by_kind(db),
            "kinds": KINDS,
            "broken": ruleset.broken,
            "sample_size": len(_sample_titles(db)),
            "sample_days": SAMPLE_DAYS,
        },
    )


@router.post("/filters/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    kind: Annotated[str, Form()] = "require_any",
    pattern: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "add",
    rule_id: Annotated[int, Form()] = 0,
) -> HTMLResponse:
    """Re-classify the sample with and without the candidate rule."""
    db = get_db(request)
    pattern = pattern.strip()

    current_patterns: dict[str, list[str]] = {k: [] for k in KINDS}
    for row in db.query("SELECT id, kind, pattern FROM filter_rules WHERE enabled = 1"):
        current_patterns[row["kind"]].append(row["pattern"])

    proposed = {k: list(v) for k, v in current_patterns.items()}
    error = None

    if action == "remove" and rule_id:
        row = db.one("SELECT kind, pattern FROM filter_rules WHERE id = ?", (rule_id,))
        if row and row["pattern"] in proposed.get(row["kind"], []):
            proposed[row["kind"]].remove(row["pattern"])
    elif pattern:
        import re

        try:
            re.compile(pattern)
        except re.error as exc:
            error = f"invalid regex: {exc}"
        else:
            if pattern not in proposed[kind]:
                proposed[kind].append(pattern)

    current_rules = RuleSet.from_patterns(current_patterns)
    proposed_rules = RuleSet.from_patterns(proposed)

    sample = _sample_titles(db)
    direct_hits = 0
    to_match: list[tuple[str, str, str, str]] = []
    to_reject: list[tuple[str, str, str, str]] = []

    probe = None
    if pattern and not error:
        import re

        probe = re.compile(pattern, re.IGNORECASE)

    for title, locations, company in sample:
        if probe is not None:
            haystack = title if kind != "location_exclude" else " ".join(locations)
            if probe.search(haystack):
                direct_hits += 1

        before = current_rules.evaluate(title, locations)
        after = proposed_rules.evaluate(title, locations)
        if before.classification == after.classification:
            continue
        entry = (title, company, before.classification, after.classification)
        if after.classification == "reject":
            to_reject.append(entry)
        else:
            to_match.append(entry)

    return partial(
        request,
        "_filter_preview.html",
        pattern=pattern,
        kind=kind,
        action=action,
        error=error,
        sample_size=len(sample),
        sample_days=SAMPLE_DAYS,
        direct_hits=direct_hits,
        to_match=to_match[:MAX_FLIPS_SHOWN],
        to_reject=to_reject[:MAX_FLIPS_SHOWN],
        more_match=max(0, len(to_match) - MAX_FLIPS_SHOWN),
        more_reject=max(0, len(to_reject) - MAX_FLIPS_SHOWN),
    )


@router.post("/filters", response_class=HTMLResponse)
async def save_rule(
    request: Request,
    kind: Annotated[str, Form()],
    pattern: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    service = get_service(request)
    pattern = pattern.strip()

    if kind in KINDS and pattern:
        import re

        try:
            re.compile(pattern)
        except re.error:
            pass  # the preview panel already says why; do not persist a broken rule
        else:
            service.db.execute(
                "INSERT OR IGNORE INTO filter_rules(kind, pattern, enabled, note, created_at) "
                "VALUES(?,?,1,?,?)",
                (kind, pattern, note.strip() or None, utcnow()),
            )
            service.classifier.purge_rule_verdicts()

    return partial(request, "_filter_rules.html", grouped=_rules_by_kind(service.db), kinds=KINDS)


@router.post("/filters/{rule_id}/toggle", response_class=HTMLResponse)
async def toggle_rule(request: Request, rule_id: int) -> HTMLResponse:
    service = get_service(request)
    service.db.execute(
        "UPDATE filter_rules SET enabled = 1 - enabled WHERE id = ?", (rule_id,)
    )
    service.classifier.purge_rule_verdicts()
    return partial(request, "_filter_rules.html", grouped=_rules_by_kind(service.db), kinds=KINDS)


@router.post("/filters/{rule_id}/delete", response_class=HTMLResponse)
async def delete_rule(request: Request, rule_id: int) -> HTMLResponse:
    service = get_service(request)
    service.db.execute("DELETE FROM filter_rules WHERE id = ?", (rule_id,))
    service.classifier.purge_rule_verdicts()
    return partial(request, "_filter_rules.html", grouped=_rules_by_kind(service.db), kinds=KINDS)


@router.get("/filters/export", response_class=PlainTextResponse)
async def export_filters(request: Request) -> PlainTextResponse:
    """Download filters.yaml so tuned rules can be committed to git."""
    _companies_yaml, filters_yaml = export_config(get_db(request))
    return PlainTextResponse(
        filters_yaml,
        headers={"Content-Disposition": 'attachment; filename="filters.yaml"'},
    )
