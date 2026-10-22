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

import time
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...classify.rules import KINDS, LOCATION_KINDS, CompiledRule, RuleSet
from ...config import export_config
from ...db import utcnow
from ..deps import get_db, get_service, parse_locations, partial, render

router = APIRouter()

SAMPLE_DAYS = 90
MAX_FLIPS_SHOWN = 40
#: A built sample serves previews this long at most, even if no job arrived.
SAMPLE_TTL_SECONDS = 300.0

# Most recent posting per distinct title. `+normalized_title` is an expression,
# so SQLite cannot take the grouping order from idx_jobs_title, which would
# fetch every row at random to get it; reading the rows in order is twice as
# fast.
SAMPLE_SQL = """
SELECT title, locations, company_slug, MAX(first_seen_at) AS seen FROM jobs
WHERE first_seen_at >= datetime('now', ?)
GROUP BY +normalized_title
ORDER BY seen DESC
"""


@dataclass(slots=True)
class _Sample:
    version: tuple
    built_at: float
    titles: list[tuple[str, list[str], str]]
    #: Each title's classification under `rules`, filled in as titles become
    #: candidates. The enabled rules rarely change mid-session; the typed
    #: pattern changes on every keystroke.
    rules: tuple = ()
    before: dict[int, str] = field(default_factory=dict)


def _rules_by_kind(db) -> dict[str, list]:
    grouped: dict[str, list] = {k: [] for k in KINDS}
    for row in db.query("SELECT * FROM filter_rules ORDER BY kind, id"):
        grouped.setdefault(row["kind"], []).append(row)
    return grouped


def _sample(request: Request) -> _Sample:
    """The preview's sample, built once per typing session.

    It reads most of `jobs` — over a second at 300k rows — and the preview
    fires on every pause in typing. Rebuilt when a job arrives or leaves, or
    after SAMPLE_TTL_SECONDS, which catches edits to rows already in it.
    """
    db = get_db(request)
    version = tuple(db.one("SELECT MAX(rowid), COUNT(*) FROM jobs"))
    now = time.monotonic()
    cached: _Sample | None = getattr(request.app.state, "filter_sample", None)
    if cached and cached.version == version and now - cached.built_at < SAMPLE_TTL_SECONDS:
        return cached

    rows = db.query(SAMPLE_SQL, (f"-{SAMPLE_DAYS} days",))
    titles = [(r["title"], parse_locations(r["locations"]), r["company_slug"]) for r in rows]
    request.app.state.filter_sample = _Sample(version, now, titles)
    return request.app.state.filter_sample


def _touches(rule: CompiledRule, title: str, locations: list[str]) -> bool:
    if rule.kind in LOCATION_KINDS:
        return any(rule.search(loc) for loc in locations)
    return rule.search(title)


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
            "sample_size": db.scalar(
                "SELECT COUNT(DISTINCT normalized_title) FROM jobs "
                "WHERE first_seen_at >= datetime('now', ?)",
                (f"-{SAMPLE_DAYS} days",),
                default=0,
            ),
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

    direct_hits = 0
    to_match: list[tuple[str, str, str, str]] = []
    to_reject: list[tuple[str, str, str, str]] = []

    probe = None
    if pattern and not error:
        import re

        probe = re.compile(pattern, re.IGNORECASE)

    # Classifying the whole sample twice took ~12s per keystroke. Only a
    # posting that a changed rule matches can classify differently, because
    # every outcome turns on which rules match — except that location_require
    # gates every located posting just by being non-empty, so starting or
    # emptying it re-checks everything.
    changed = [
        CompiledRule.compile(0, k, p)
        for k in KINDS
        for p in set(current_patterns[k]) ^ set(proposed[k])
    ]
    recheck_all = any(
        rule.kind == "location_require"
        and not (current_patterns["location_require"] and proposed["location_require"])
        for rule in changed
    )
    # A typed title pattern's direct hits are exactly the postings it can
    # flip, so it is searched once rather than once more as a change.
    reuse_hits = probe is not None and kind not in LOCATION_KINDS
    if reuse_hits:
        changed = [r for r in changed if (r.kind, r.pattern) != (kind, pattern)]

    # A cleared or uncompilable pattern renders without the sample at all.
    titles: list[tuple[str, list[str], str]] = []
    before_cache: dict[int, str] = {}
    if probe is not None or changed:
        sample = _sample(request)
        rules_key = tuple(tuple(sorted(current_patterns[k])) for k in KINDS)
        if sample.rules != rules_key:
            sample.rules, sample.before = rules_key, {}
        titles, before_cache = sample.titles, sample.before

    for index, (title, locations, company) in enumerate(titles):
        hit = False
        if probe is not None:
            haystack = " ".join(locations) if kind in LOCATION_KINDS else title
            hit = bool(probe.search(haystack))
            if hit:
                direct_hits += 1

        if not (
            recheck_all
            or (reuse_hits and hit)
            or (changed and any(_touches(rule, title, locations) for rule in changed))
        ):
            continue

        before = before_cache.get(index)
        if before is None:
            before = before_cache[index] = current_rules.evaluate(title, locations).classification
        after = proposed_rules.evaluate(title, locations).classification
        if before == after:
            continue
        entry = (title, company, before, after)
        if after == "reject":
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
        sample_size=len(titles),
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
