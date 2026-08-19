"""Eightfold AI career sites (§11).

    GET {base}{path}?domain={domain}&start=0&num=50&query=intern

Written as a general adapter rather than a `direct.netflix` module because
Eightfold is a platform, not one company's bespoke page — the same endpoint
shape serves every tenant, so one adapter covers all of them.

Two flavours are in the wild and both are handled here:

* the classic one at `/api/apply/v2/jobs`, which answers with `positions` and
  `count` at the top level and names its fields in snake_case;
* the newer "pcsx" one at `/api/pcsx/search` (Microsoft), which wraps the same
  two keys in a `data` envelope, names its fields in camelCase, and **ignores
  `num`** — it always returns 10 per page.

That last one matters: a short page is the classic flavour's end-of-results
signal, so trusting it would have stopped Microsoft after the first ten. The
short-page break is therefore only used when `count` is missing (§13.10).

Config: {base: https://explore.jobs.netflix.net, domain: netflix.com}
Optional: {path: /api/apply/v2/jobs, query: intern, num: 50, sort_by: relevance}
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..models import FetchResult
from .base import (
    AdapterError,
    FetchContext,
    absolute_url,
    build_postings,
    conditional_json,
    register,
    require_list,
)

PAGE_SIZE = 50
MAX_PAGES = 30
DEFAULT_PATH = "/api/apply/v2/jobs"


@register("eightfold")
class EightfoldAdapter:
    name: ClassVar[str] = "eightfold"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.require("base")).rstrip("/")
        domain = str(ctx.require("domain")).strip()
        query = str(ctx.config.get("query", ""))
        page_size = int(ctx.config.get("num", PAGE_SIZE))
        path = "/" + str(ctx.config.get("path", DEFAULT_PATH)).lstrip("/")
        sort_by = str(ctx.config.get("sort_by", "relevance"))

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        start = 0
        total: int | None = None
        etag = last_modified = None

        for page in range(MAX_PAGES):
            url = (
                f"{base}{path}?domain={domain}"
                f"&start={start}&num={page_size}&query={query}&sort_by={sort_by}"
            )
            payload, response = await conditional_json(ctx, url, use_conditional=(page == 0))
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            body = _envelope(payload)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                if isinstance(body, dict) and isinstance(body.get("count"), int):
                    total = body["count"]

            positions = require_list(body, "positions", self.name)

            new_on_page = 0
            for position in positions:
                mapped = self._map(position, base)
                if mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            start += len(positions)
            if not positions or new_on_page == 0:
                break
            if isinstance(total, int):
                if start >= total:
                    break
            # Without a total, a short page is the only end-of-results signal.
            elif len(positions) < page_size:
                break
            if ctx.max_pages is not None and page + 1 >= ctx.max_pages:
                break
        else:
            raise AdapterError(
                f"pagination exceeded {MAX_PAGES} pages for {domain}", adapter=self.name
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    def _map(self, position: Any, base: str) -> dict[str, Any]:
        if not isinstance(position, dict):
            raise AdapterError(
                f"expected positions entries to be objects, got {type(position).__name__}",
                adapter=self.name,
                payload_snippet=repr(position)[:200],
            )

        job_id = position.get("id")
        url = _pick(position, "canonicalPositionUrl", "positionUrl") or (
            f"{base}/careers/job/{job_id}" if job_id else None
        )
        if not url:
            raise AdapterError(
                "position has neither canonicalPositionUrl/positionUrl nor id",
                adapter=self.name,
                payload_snippet=repr(position)[:300],
            )

        locations = position.get("locations")
        if not isinstance(locations, list):
            locations = [position["location"]] if position.get("location") else []

        return {
            "req_id": _pick(
                position, "display_job_id", "displayJobId", "ats_job_id", "atsJobId"
            )
            or job_id,
            "title": _pick(position, "name", "posting_name"),
            "url": absolute_url(base, str(url)),
            "locations": [_tidy(x) for x in locations if x],
            "posted_at": _epoch(_pick(position, "t_create", "postedTs", "creationTs")),
            "department": position.get("department"),
        }


def _envelope(payload: Any) -> Any:
    """Unwrap the pcsx `data` envelope; classic responses pass straight through."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _pick(node: dict[str, Any], *keys: str) -> Any:
    """First key present with a usable value — the two flavours disagree on names."""
    for key in keys:
        value = node.get(key)
        if value not in (None, "", []):
            return value
    return None


def _tidy(value: Any) -> str:
    """'Los Gatos,California,United States of America' -> 'Los Gatos, California, USA'."""
    text = str(value)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if parts and parts[-1] in ("United States of America", "United States"):
        parts[-1] = "USA"
    return ", ".join(parts)


def _epoch(value: Any) -> str | None:
    from datetime import UTC, datetime

    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None
