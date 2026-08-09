"""Apple — jobs.apple.com search API (§5 recipe).

    POST https://jobs.apple.com/api/v1/search
    {"query": "intern", "filters": {}, "page": 1, "locale": "en-us", "sort": "newest"}

Two traps, both silent:

* A `Referer` on jobs.apple.com is required, or the endpoint 404s.
* The `format` object is required. **Without it the API returns HTTP 200 with
  `totalRecords: 0`** — a perfectly well-formed empty board. That is exactly the
  failure §13.10 exists to prevent, so it is sent unconditionally rather than
  left to config.

No cookies, no auth. Apply URLs are assembled from `positionId` and the URL-slug
variant of the title (`transformedPostingTitle`); the API never returns one whole.

Query note: `intern` matches ~1,825 roles because Apple's search is fuzzy enough
to hit "IN-Business Expert". `internship` returns ~100 and is the right default.

Config: {base: https://jobs.apple.com, query: internship}
"""

from __future__ import annotations

from typing import Any, ClassVar

from ...models import FetchResult
from ..base import (
    AdapterError,
    FetchContext,
    build_postings,
    conditional_json,
    register,
    require_list,
)

MAX_PAGES = 25

# Not cosmetic: omitting this yields 200 with an empty result set.
DATE_FORMAT = {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}


@register("direct.apple")
class AppleAdapter:
    name: ClassVar[str] = "direct.apple"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.config.get("base", "https://jobs.apple.com")).rstrip("/")
        query = str(ctx.config.get("query", "internship"))
        locale = str(ctx.config.get("locale", "en-us"))

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{base}/{locale}/search",
        }

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        etag = last_modified = None

        for page in range(1, MAX_PAGES + 1):
            body = {
                "query": query,
                "filters": ctx.config.get("filters") or {},
                "page": page,
                "locale": locale,
                "sort": "newest",
                "format": DATE_FORMAT,
            }
            payload, response = await conditional_json(
                ctx,
                f"{base}/api/v1/search",
                method="POST",
                json_body=body,
                headers=headers,
                use_conditional=(page == 1),
            )
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            res = payload.get("res") if isinstance(payload, dict) else None
            if not isinstance(res, dict):
                raise AdapterError(
                    "response has no 'res' object -- the search API shape changed",
                    adapter=self.name,
                    payload_snippet=repr(payload)[:300],
                )

            if page == 1:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                if isinstance(res.get("totalRecords"), int):
                    total = res["totalRecords"]

            results = require_list(res, "searchResults", self.name)

            new_on_page = 0
            for result in results:
                mapped = self._map(result, base, locale)
                if mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            if not results or new_on_page == 0:
                break
            if isinstance(total, int) and len(seen) >= total:
                break
        else:
            raise AdapterError(
                f"pagination exceeded {MAX_PAGES} pages for query {query!r}",
                adapter=self.name,
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    def _map(self, result: Any, base: str, locale: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise AdapterError(
                f"expected searchResults entries to be objects, got {type(result).__name__}",
                adapter=self.name,
                payload_snippet=repr(result)[:200],
            )

        position_id = result.get("positionId") or result.get("id")
        slug = result.get("transformedPostingTitle") or ""
        if not position_id:
            raise AdapterError(
                "search result has no positionId -- cannot build an apply URL",
                adapter=self.name,
                payload_snippet=repr(result)[:300],
            )

        return {
            "req_id": result.get("reqId") or result.get("jobPositionId") or position_id,
            "title": result.get("postingTitle") or result.get("posting_name"),
            "url": f"{base}/{locale}/details/{position_id}/{slug}".rstrip("/"),
            "locations": _locations(result),
            "posted_at": result.get("postingDate") or result.get("postDateInGMT"),
            "department": result.get("team", {}).get("teamName")
            if isinstance(result.get("team"), dict)
            else result.get("team"),
        }


def _locations(result: dict[str, Any]) -> list[str]:
    nodes = result.get("locations")
    if not isinstance(nodes, list):
        return []
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            label = node.get("name") or node.get("city") or node.get("metro")
            if label:
                country = node.get("countryName") or node.get("countryID")
                out.append(f"{label}, {country}" if country and country not in str(label) else str(label))
    return out
