"""Companies — the registry (§10).

Two things here do real work. **Test fetch** runs a source once and shows what
came back without persisting anything, which is how you tell a wrong board token
from an empty board in five seconds. **Discover** takes a careers URL, identifies
the platform behind it, verifies the guess against the live endpoint, and hands
back a config block ready to save.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...adapters.base import AdapterError, FetchContext, get_adapter
from ...discover import discover
from ...normalize import merge_key, normalize_title
from ..deps import get_db, get_service, partial, render

router = APIRouter()


@router.get("/companies", response_class=HTMLResponse)
async def companies(request: Request) -> HTMLResponse:
    db = get_db(request)
    rows = db.query(
        """
        SELECT c.*, COUNT(s.id) AS source_count,
               SUM(CASE WHEN s.enabled = 1 THEN 1 ELSE 0 END) AS enabled_sources
        FROM companies c
        LEFT JOIN sources s ON s.company_slug = c.slug
        GROUP BY c.slug
        ORDER BY CASE c.tier WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END,
                 c.display_name
        """
    )
    sources = db.query(
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM jobs j WHERE j.source_id = s.id) AS job_count "
        "FROM sources s ORDER BY s.company_slug, s.priority, s.id"
    )
    by_company: dict[str, list] = {}
    for row in sources:
        by_company.setdefault(row["company_slug"], []).append(row)

    return render(
        request,
        "companies.html",
        {"companies": rows, "sources": by_company},
    )


@router.post("/companies/{slug}/test", response_class=HTMLResponse)
async def test_fetch(
    request: Request, slug: str, adapter: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Run a source once and show the parsed postings. Persists nothing."""
    service = get_service(request)
    db = service.db

    query = "SELECT * FROM sources WHERE company_slug = ?"
    args: list = [slug]
    if adapter:
        query += " AND adapter = ?"
        args.append(adapter)
    query += " ORDER BY priority LIMIT 1"

    source = db.one(query, args)
    if source is None:
        return partial(
            request, "_test_result.html", slug=slug, error="no such source", postings=[]
        )

    config = json.loads(source["adapter_config"] or "{}")
    ctx = FetchContext(
        client=service.client,
        config=config,
        company_slug=slug,
        adapter_name=source["adapter"],
        probe=True,  # never writes conditional-request state
    )

    try:
        result = await get_adapter(source["adapter"]).fetch(ctx)
    except AdapterError as exc:
        return partial(
            request, "_test_result.html", slug=slug, adapter=source["adapter"],
            error=str(exc), postings=[],
        )
    # A probe failing is exactly what the operator opened this screen to see.
    except Exception as exc:
        return partial(
            request, "_test_result.html", slug=slug, adapter=source["adapter"],
            error=f"{type(exc).__name__}: {exc}", postings=[],
        )

    known = {
        r["merge_key"]
        for r in db.query("SELECT merge_key FROM jobs WHERE company_slug = ?", (slug,))
    }
    rows = []
    for posting in result.postings[:60]:
        mk = merge_key(slug, posting.title)
        decision = service.classifier.explain(posting.title, posting.locations)
        rows.append(
            {
                "title": posting.title,
                "url": posting.url,
                "locations": posting.locations,
                "req_id": posting.req_id,
                "normalized": normalize_title(posting.title),
                "classification": decision.classification,
                "known": mk in known,
            }
        )

    return partial(
        request,
        "_test_result.html",
        slug=slug,
        adapter=source["adapter"],
        error=None,
        total=len(result.postings),
        not_modified=result.not_modified,
        postings=rows,
    )


@router.post("/companies/{slug}/toggle", response_class=HTMLResponse)
async def toggle_company(request: Request, slug: str) -> HTMLResponse:
    get_db(request).execute(
        "UPDATE companies SET enabled = 1 - enabled WHERE slug = ?", (slug,)
    )
    return await companies(request)


@router.post("/sources/{source_id}/toggle", response_class=HTMLResponse)
async def toggle_source(request: Request, source_id: int) -> HTMLResponse:
    # Disabled, never deleted: `jobs.source_id` references this row, and job
    # history is not something we throw away (§13.4).
    get_db(request).execute(
        "UPDATE sources SET enabled = 1 - enabled WHERE id = ?", (source_id,)
    )
    return await companies(request)


@router.post("/discover", response_class=HTMLResponse)
async def discover_url(
    request: Request, url: Annotated[str, Form()]
) -> HTMLResponse:
    service = get_service(request)
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    result = await discover(url, service.client)
    return partial(request, "_discover_result.html", result=result, url=url)


@router.post("/companies", response_class=HTMLResponse)
async def add_company(
    request: Request,
    slug: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    adapter: Annotated[str, Form()],
    config: Annotated[str, Form()],
    tier: Annotated[str, Form()] = "warm",
    careers_url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save a discovered source. The new source seeds silently on its first poll."""
    db = get_db(request)
    slug = slug.strip().lower()

    try:
        parsed = json.loads(config or "{}")
    except ValueError:
        parsed = {}

    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO companies(slug, display_name, tier, enabled, careers_url) "
            "VALUES(?,?,?,1,?)",
            (slug, display_name.strip() or slug, tier, careers_url.strip() or None),
        )
        conn.execute(
            "INSERT OR IGNORE INTO sources(company_slug, adapter, adapter_config, priority, "
            "enabled, fallback_config) VALUES(?,?,?,1,1,'{}')",
            (slug, adapter, json.dumps(parsed, sort_keys=True)),
        )

    return await companies(request)
