"""Amazon — www.amazon.jobs search JSON (§5 recipe).

    GET https://www.amazon.jobs/en/search.json
        ?base_query=software+intern&result_limit=100&offset=0&sort=recent

The endpoint the search page itself calls. No auth, no cookies. `job_path` is a
fragment that must be joined to the base — sending it raw to Discord produces an
unclickable alert.

Config: {base: https://www.amazon.jobs, query: "intern software"}
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote_plus

from ...models import FetchResult
from ..base import (
    AdapterError,
    FetchContext,
    absolute_url,
    build_postings,
    conditional_json,
    register,
    require_list,
)

PAGE_SIZE = 100
MAX_PAGES = 20


@register("direct.amazon")
class AmazonAdapter:
    name: ClassVar[str] = "direct.amazon"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.config.get("base", "https://www.amazon.jobs")).rstrip("/")
        query = str(ctx.config.get("query", "intern"))
        page_size = int(ctx.config.get("limit", PAGE_SIZE))

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        hits: int | None = None
        etag = last_modified = None

        for page in range(MAX_PAGES):
            url = (
                f"{base}/en/search.json?base_query={quote_plus(query)}"
                f"&result_limit={page_size}&offset={offset}&sort=recent"
            )
            payload, response = await conditional_json(ctx, url, use_conditional=(page == 0))
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if isinstance(payload, dict) and payload.get("error"):
                raise AdapterError(
                    f"search.json reported an error: {payload['error']}",
                    adapter=self.name,
                )

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                if isinstance(payload, dict) and isinstance(payload.get("hits"), int):
                    hits = payload["hits"]

            jobs = require_list(payload, "jobs", self.name)

            new_on_page = 0
            for job in jobs:
                mapped = self._map(job, base)
                if mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            offset += len(jobs)
            if not jobs or new_on_page == 0:
                break
            if isinstance(hits, int) and offset >= hits:
                break
            if len(jobs) < page_size:
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

    def _map(self, job: Any, base: str) -> dict[str, Any]:
        if not isinstance(job, dict):
            raise AdapterError(
                f"expected job objects, got {type(job).__name__}",
                adapter=self.name,
                payload_snippet=repr(job)[:200],
            )
        path = job.get("job_path")
        if not path:
            raise AdapterError(
                "job has no job_path -- cannot build an apply URL",
                adapter=self.name,
                payload_snippet=repr(job)[:300],
            )

        locations: list[str] = []
        for key in ("normalized_location", "location"):
            if job.get(key):
                locations.append(str(job[key]))
        extra = job.get("locations")
        if isinstance(extra, list):
            locations.extend(str(x) for x in extra if x)

        return {
            "req_id": job.get("id_icims") or job.get("id"),
            "title": job.get("title"),
            "url": absolute_url(base, str(path)),
            "locations": locations,
            # A human date string such as "May 13, 2026". Display only (§13.3).
            "posted_at": job.get("posted_date") or job.get("updated_time"),
            "department": job.get("job_category") or job.get("team"),
        }
