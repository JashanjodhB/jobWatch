"""Eightfold AI career sites (§11).

    GET {base}/api/apply/v2/jobs?domain={domain}&start=0&num=50&query=intern

Written as a general adapter rather than a `direct.netflix` module because
Eightfold is a platform, not one company's bespoke page — the same endpoint
shape serves every tenant, so one adapter covers all of them.

Config: {base: https://explore.jobs.netflix.net, domain: netflix.com}
Optional: {query: intern, num: 50}
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


@register("eightfold")
class EightfoldAdapter:
    name: ClassVar[str] = "eightfold"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.require("base")).rstrip("/")
        domain = str(ctx.require("domain")).strip()
        query = str(ctx.config.get("query", ""))
        page_size = int(ctx.config.get("num", PAGE_SIZE))

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        start = 0
        total: int | None = None
        etag = last_modified = None

        for page in range(MAX_PAGES):
            url = (
                f"{base}/api/apply/v2/jobs?domain={domain}"
                f"&start={start}&num={page_size}&query={query}&sort_by=relevance"
            )
            payload, response = await conditional_json(ctx, url, use_conditional=(page == 0))
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                if isinstance(payload, dict) and isinstance(payload.get("count"), int):
                    total = payload["count"]

            positions = require_list(payload, "positions", self.name)

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
            if isinstance(total, int) and start >= total:
                break
            if len(positions) < page_size:
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
        url = position.get("canonicalPositionUrl") or (
            f"{base}/careers/job/{job_id}" if job_id else None
        )
        if not url:
            raise AdapterError(
                "position has neither canonicalPositionUrl nor id",
                adapter=self.name,
                payload_snippet=repr(position)[:300],
            )

        locations = position.get("locations")
        if not isinstance(locations, list):
            locations = [position["location"]] if position.get("location") else []

        return {
            "req_id": position.get("display_job_id") or position.get("ats_job_id") or job_id,
            "title": position.get("name") or position.get("posting_name"),
            "url": absolute_url(base, str(url)),
            "locations": [_tidy(x) for x in locations if x],
            "posted_at": _epoch(position.get("t_create")),
            "department": position.get("department"),
        }


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
