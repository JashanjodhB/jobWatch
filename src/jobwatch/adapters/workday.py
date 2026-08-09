"""Workday CXS — where most of the F500 lives (§11).

    POST {host}/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"}

Three things bite, all of them called out in the spec:

* **Headers.** Without both `Accept: application/json` and
  `Content-Type: application/json` the endpoint returns the HTML shell instead.
* **`postedOn` is a human string** ("Posted Today", "Posted 30+ Days Ago").
  Never parse it for newness -- it is carried as display text only (§13.3).
* **Pagination.** offset/limit, and some tenants cap `limit` at 20. The cap does
  not truncate the page — it returns **zero** postings and drops `total`
  entirely, which is indistinguishable from an empty board unless you know to
  look. So page 0 is a probe: if an oversized limit comes back empty, we retry
  once at 20 before believing it.

Apply URLs are built from `externalPath`; the endpoint never returns a whole one.

A full scan of a large tenant is expensive — NVIDIA's "intern" search is ~900
postings, or 46 requests at the capped page size. Set `min_interval_seconds` in
the source config to keep heavy tenants off the hot-tier cadence; hammering one
host 46 times a minute from a residential IP is how you get blocked (§13.5).
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from ..models import FetchResult
from .base import (
    AdapterError,
    FetchContext,
    build_postings,
    conditional_json,
    register,
    require_list,
)

DEFAULT_LIMIT = 20
SAFE_LIMIT = 20          # the page size every tenant observed so far accepts
MAX_PAGES = 80
PAGE_DELAY_SECONDS = 0.25


@register("workday")
class WorkdayAdapter:
    name: ClassVar[str] = "workday"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        host = str(ctx.require("host")).rstrip("/")
        tenant = str(ctx.require("tenant")).strip()
        site = str(ctx.require("site")).strip()
        locale = str(ctx.config.get("locale", "en-US"))
        search_text = ctx.config.get("search_text", "")
        requested = int(ctx.config.get("limit", DEFAULT_LIMIT))

        url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}

        async def page_request(limit: int, offset: int, conditional: bool):
            body: dict[str, Any] = {
                "appliedFacets": ctx.config.get("applied_facets") or {},
                "limit": limit,
                "offset": offset,
                "searchText": search_text,
            }
            return await conditional_json(
                ctx, url, method="POST", json_body=body, headers=headers,
                use_conditional=conditional,
            )

        page_size = requested
        payload, response = await page_request(page_size, 0, True)
        if payload is None:
            return FetchResult(not_modified=True, adapter=self.name)

        postings = require_list(payload, "jobPostings", self.name)
        total = payload.get("total") if isinstance(payload, dict) else None

        # An oversized limit is rejected by returning nothing at all. Retry once
        # at the safe page size before concluding the board is empty -- treating
        # this as "no postings" is precisely the silent failure §13.10 forbids.
        if not postings and page_size > SAFE_LIMIT:
            page_size = SAFE_LIMIT
            payload, response = await page_request(page_size, 0, False)
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)
            postings = require_list(payload, "jobPostings", self.name)
            total = payload.get("total") if isinstance(payload, dict) else None

        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")

        items: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        offset = 0
        pages = 0

        while True:
            pages += 1
            new_on_page = 0
            for posting in postings:
                mapped = self._map(posting, host, site, locale)
                # Tenants repeat rows across pages when the board shifts
                # mid-scan; externalPath is stable enough to detect that.
                if mapped["url"] in seen_paths:
                    continue
                seen_paths.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            offset += len(postings)
            if not postings or new_on_page == 0:
                break
            if isinstance(total, int) and offset >= total:
                break
            if len(postings) < page_size and not isinstance(total, int):
                break
            if pages >= MAX_PAGES:
                raise AdapterError(
                    f"pagination exceeded {MAX_PAGES} pages at {tenant}/{site} "
                    f"(collected {len(items)} of {total}) -- refusing to keep hammering",
                    adapter=self.name,
                )

            await asyncio.sleep(PAGE_DELAY_SECONDS)
            payload, _response = await page_request(page_size, offset, False)
            if payload is None:
                break
            postings = require_list(payload, "jobPostings", self.name)

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    def _map(self, posting: Any, host: str, site: str, locale: str) -> dict[str, Any]:
        if not isinstance(posting, dict):
            raise AdapterError(
                f"expected jobPostings entries to be objects, got {type(posting).__name__}",
                adapter=self.name,
                payload_snippet=repr(posting)[:200],
            )

        external_path = posting.get("externalPath")
        if not external_path:
            raise AdapterError(
                "jobPostings entry has no externalPath -- cannot build an apply URL",
                adapter=self.name,
                payload_snippet=repr(posting)[:300],
            )

        return {
            "req_id": _req_id(posting, str(external_path)),
            "title": posting.get("title"),
            "url": f"{host}/{locale}/{site}{external_path}",
            "locations": _locations(posting),
            # Human text such as "Posted 30+ Days Ago". Display only.
            "posted_at": posting.get("postedOn"),
            "department": posting.get("jobFamily"),
        }


def _req_id(posting: dict[str, Any], external_path: str) -> str:
    bullets = posting.get("bulletFields")
    if isinstance(bullets, list):
        for bullet in bullets:
            if bullet and not str(bullet).lower().startswith("posted"):
                return str(bullet)
    # externalPath tails look like ".../Software-Engineer-Intern_JR1993284"
    tail = external_path.rsplit("_", 1)
    return tail[-1] if len(tail) == 2 and tail[-1] else external_path


def _locations(posting: dict[str, Any]) -> list[str]:
    out: list[str] = []
    primary = posting.get("locationsText") or posting.get("locationText")
    if primary:
        out.append(str(primary))
    extra = posting.get("additionalLocations")
    if isinstance(extra, list):
        out.extend(str(x) for x in extra if x)
    return out
