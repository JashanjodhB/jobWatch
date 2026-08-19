"""Jibe careers sites — the search layer in front of iCIMS (§11).

    GET {base}/api/jobs?keywords=intern&page=1&limit=100&sortBy=relevance

Jibe is a careers-site front end, not an ATS: the postings it serves belong to
the ATS behind it, which each record names in `ats_code` (`icims` for AMD). That
is why this is a platform adapter rather than a `direct.amd` module -- one
endpoint shape serves every tenant, and iCIMS is the largest platform the
registry does not otherwise reach.

`apply_url` comes back absolute and pointing at the real ATS
(`globalcampus-amd.icims.com/jobs/76895/login`), so alerts link where a human
actually applies and nothing has to be assembled by hand.

Two things worth not rediscovering:

* `limit` is honoured up to **100**; 200 returns an empty list rather than an
  error, so the cap is not negotiable and asking for more silently loses the
  whole page.
* `totalCount` is the real total, not the page size -- it only looked like a
  page size on the first probe because that search genuinely had ten hits.

Config: {base: https://careers.amd.com}
Optional: {query: intern, limit: 100, sort_by: relevance}
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import urlencode

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

PAGE_SIZE = 100
MAX_LIMIT = 100
MAX_PAGES = 30


@register("jibe")
class JibeAdapter:
    name: ClassVar[str] = "jibe"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.require("base")).rstrip("/")
        query = str(ctx.config.get("query", "intern"))
        sort_by = str(ctx.config.get("sort_by", "relevance"))
        limit = min(int(ctx.config.get("limit", PAGE_SIZE)), MAX_LIMIT)

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        etag = last_modified = None

        for page in range(MAX_PAGES):
            params = urlencode({
                "keywords": query,
                "page": page + 1,          # 1-based
                "limit": limit,
                "sortBy": sort_by,
                "descending": "false",
                "internal": "false",
            })
            payload, response = await conditional_json(
                ctx, f"{base}/api/jobs?{params}", use_conditional=(page == 0)
            )
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                if isinstance(payload, dict) and isinstance(payload.get("totalCount"), int):
                    total = payload["totalCount"]

            jobs = require_list(payload, "jobs", self.name)

            new_on_page = 0
            for job in jobs:
                mapped = self._map(job, base)
                if mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            if not jobs or new_on_page == 0:
                break
            if isinstance(total, int) and len(seen) >= total:
                break
            if len(jobs) < limit:
                break
            if ctx.max_pages is not None and page + 1 >= ctx.max_pages:
                break
        else:
            raise AdapterError(
                f"pagination exceeded {MAX_PAGES} pages for {base}", adapter=self.name
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    def _map(self, job: Any, base: str) -> dict[str, Any]:
        if not isinstance(job, dict):
            raise AdapterError(
                f"expected jobs entries to be objects, got {type(job).__name__}",
                adapter=self.name,
                payload_snippet=repr(job)[:200],
            )
        # Every posting is wrapped: {"data": {...}}. A bare record would mean the
        # envelope moved, which must be loud rather than an empty board (§13.10).
        data = job.get("data")
        if not isinstance(data, dict):
            raise AdapterError(
                "job entry has no 'data' object -- the envelope changed",
                adapter=self.name,
                payload_snippet=repr(job)[:300],
            )

        apply_url = data.get("apply_url")
        slug = data.get("slug") or data.get("req_id")
        if not apply_url and not slug:
            raise AdapterError(
                "job has neither apply_url nor slug -- cannot build an apply URL",
                adapter=self.name,
                payload_snippet=repr(data)[:300],
            )

        return {
            "req_id": _str_or_none(data.get("req_id")),
            "title": data.get("title"),
            "url": absolute_url(base, str(apply_url or f"/careers-home/jobs/{slug}")),
            "locations": _places(data),
            "posted_at": data.get("posted_date") or data.get("create_date"),
            "department": _department(data.get("category")),
        }


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _places(data: dict[str, Any]) -> list[str]:
    """`full_location` is the display string; `locations` appears on multi-site reqs."""
    extra = data.get("locations")
    if isinstance(extra, list) and extra:
        out = []
        for node in extra:
            if isinstance(node, str):
                out.append(node.strip())
            elif isinstance(node, dict):
                text = node.get("full_location") or node.get("name") or node.get("city")
                if text:
                    out.append(str(text).strip())
        if out:
            return out

    for key in ("full_location", "short_location", "location_name"):
        value = data.get(key)
        if value:
            return [str(value).strip()]
    return []


def _department(category: Any) -> str | None:
    """`category` is a list of strings, each arriving with a leading space."""
    if isinstance(category, list):
        parts = [str(c).strip() for c in category if str(c).strip()]
        return ", ".join(parts) or None
    if isinstance(category, str) and category.strip():
        return category.strip()
    return None
