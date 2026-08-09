"""Uber — www.uber.com careers search API (§5 recipe).

    POST https://www.uber.com/api/loadSearchJobsResults?localeCode=en
    {"params": {"query": "intern"}, "page": 0, "limit": 100}

Requires an `x-csrf-token` header, but any non-empty value satisfies it — the
endpoint checks for presence, not validity. No cookies, no account.

Config: {base: https://www.uber.com, query: intern}
"""

from __future__ import annotations

import html
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

PAGE_SIZE = 100
MAX_PAGES = 20


@register("direct.uber")
class UberAdapter:
    name: ClassVar[str] = "direct.uber"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.config.get("base", "https://www.uber.com")).rstrip("/")
        query = str(ctx.config.get("query", "intern"))
        page_size = int(ctx.config.get("limit", PAGE_SIZE))

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-csrf-token": "x",
        }

        items: list[dict[str, Any]] = []
        seen: set[str] = set()

        for page in range(MAX_PAGES):
            body = {
                "params": {"query": query, **(ctx.config.get("params") or {})},
                "page": page,
                "limit": page_size,
            }
            payload, _response = await conditional_json(
                ctx,
                f"{base}/api/loadSearchJobsResults?localeCode=en",
                method="POST",
                json_body=body,
                headers=headers,
                use_conditional=False,  # POST search results are never cacheable
            )
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if not isinstance(payload, dict) or payload.get("status") != "success":
                raise AdapterError(
                    f"search returned status {payload.get('status') if isinstance(payload, dict) else '?'!r}",
                    adapter=self.name,
                    payload_snippet=repr(payload)[:300],
                )

            data = payload.get("data")
            if not isinstance(data, dict):
                raise AdapterError(
                    "response has no 'data' object -- the API shape changed",
                    adapter=self.name,
                    payload_snippet=repr(payload)[:300],
                )

            results = require_list(data, "results", self.name)

            new_on_page = 0
            for result in results:
                mapped = self._map(result, base)
                if mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            if not results or new_on_page == 0 or len(results) < page_size:
                break
        else:
            raise AdapterError(
                f"pagination exceeded {MAX_PAGES} pages for query {query!r}",
                adapter=self.name,
            )

        return FetchResult(postings=build_postings(items, self.name), adapter=self.name)

    def _map(self, result: Any, base: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise AdapterError(
                f"expected results entries to be objects, got {type(result).__name__}",
                adapter=self.name,
                payload_snippet=repr(result)[:200],
            )
        job_id = result.get("id")
        if not job_id:
            raise AdapterError(
                "result has no id -- cannot build an apply URL",
                adapter=self.name,
                payload_snippet=repr(result)[:300],
            )

        locations = [_place(result.get("location"))]
        for extra in result.get("allLocations") or []:
            locations.append(_place(extra))

        return {
            "req_id": str(job_id),
            # Titles and teams come back HTML-escaped ("Business &amp; Sales").
            "title": html.unescape(str(result.get("title") or "")),
            "url": f"{base}/global/en/careers/list/{job_id}/",
            "locations": [x for x in locations if x],
            "posted_at": result.get("creationDate"),
            "department": html.unescape(str(result.get("department") or "")) or None,
        }


def _place(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    parts = [node.get("city"), node.get("region"), node.get("countryName")]
    return ", ".join(str(p) for p in parts if p)
