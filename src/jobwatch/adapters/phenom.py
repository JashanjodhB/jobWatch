"""Phenom People career sites (§11).

    POST {base}/widgets
    {"ddoKey": "refineSearch", "keywords": "intern", "from": 0, "size": 100, ...}

Phenom is the largest platform the registry could not reach: eleven of the
companies in the 2026-08-22 sweep run on it (Cisco, Splunk, Chewy, CVS Health,
Genentech, UPS, GE HealthCare, Lowe's, Niantic, McKesson and more). It is a
platform rather than one company's page, so one adapter serves every tenant --
the only per-tenant value is the host.

Three things that cost time to find and are not guessable:

* **The endpoint is a POST to `/widgets`, not a GET.** A GET to the same path
  returns the HTML shell, which is why URL fingerprinting never finds it.
* **`ddoKey` selects what the call returns, and only `refineSearch` returns
  jobs.** The key the page itself sends first is `eagerLoadRefineSearchSession`,
  which answers `{"refineSearch": {"tokenAvailable": ...}}` and no postings at
  all -- capture that one and the board looks empty. `refineSearch` needs no
  token and no session.
* **`Origin`/`Referer` must be the tenant's own host.** Without them some
  tenants answer 403.

`size` is honoured (unlike Jibe's cap), and paging is `from`, an absolute offset.
A page past the end comes back as an empty list rather than an error.

`applyUrl` is absolute and points at the real ATS behind Phenom -- often Workday
(`wd5.myworkdaysite.com/...`) -- so alerts link where a human actually applies.

Config: {base: https://careers.chewy.com}
Optional: {query: intern, size: 100, country: us, lang: en_us, site_type: external}
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
)

PAGE_SIZE = 100
MAX_PAGES = 30

# Only this key returns postings. See the module docstring.
DDO_KEY = "refineSearch"


@register("phenom")
class PhenomAdapter:
    name: ClassVar[str] = "phenom"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        base = str(ctx.require("base")).rstrip("/")
        query = str(ctx.config.get("query", "intern"))
        size = int(ctx.config.get("size", PAGE_SIZE))
        country = str(ctx.config.get("country", "us"))
        lang = str(ctx.config.get("lang", "en_us"))
        site_type = str(ctx.config.get("site_type", "external"))

        url = f"{base}/widgets"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Some tenants 403 a request that does not look like it came from
            # their own search page.
            "Origin": base,
            "Referer": f"{base}/{country}/{lang.split('_')[0]}/search-results",
        }

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        etag = last_modified = None

        for page in range(MAX_PAGES):
            body = {
                "ddoKey": DDO_KEY,
                "lang": lang,
                "deviceType": "desktop",
                "country": country,
                "pageName": "search-results",
                "siteType": site_type,
                "keywords": query,
                "sortBy": "",
                "subsearch": "",
                "from": page * size,
                "size": size,
                "jobs": True,
                "counts": True,
                "irs": False,
                "clearAll": False,
                "global": True,
                "jdsource": "facets",
                "isSliderEnable": True,
                "pageId": "page13-ds",
                "all_fields": ["category", "city", "state", "type"],
                "selected_fields": {},
                "locationData": {
                    "sliderRadius": 25,
                    "aboveMaxRadius": False,
                    "LocationUnit": "miles",
                },
            }

            payload, response = await conditional_json(
                ctx, url, method="POST", json_body=body, headers=headers,
                use_conditional=(page == 0),
            )
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")

            jobs = self._jobs(payload)

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
            if len(jobs) < size:
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

    def _jobs(self, payload: Any) -> list[Any]:
        """Unwrap {"refineSearch": {"data": {"jobs": [...]}}}.

        A missing envelope is raised rather than treated as an empty board: the
        two are indistinguishable downstream and only one is meant to be silent
        (§13.10). An envelope that is present but holds no jobs is a real empty
        result and returns [].
        """
        if not isinstance(payload, dict):
            raise AdapterError(
                f"expected a JSON object, got {type(payload).__name__}",
                adapter=self.name, payload_snippet=repr(payload)[:200],
            )
        refine = payload.get(DDO_KEY)
        if not isinstance(refine, dict):
            raise AdapterError(
                f"response has no '{DDO_KEY}' object -- the envelope changed",
                adapter=self.name, payload_snippet=repr(payload)[:300],
            )
        data = refine.get("data")
        if not isinstance(data, dict):
            # This is what a session-gated ddoKey answers; name it, because the
            # symptom is an empty board rather than an error.
            raise AdapterError(
                f"'{DDO_KEY}' carries no 'data' object (keys: {sorted(refine)}) -- "
                "the endpoint answered a session probe rather than a search",
                adapter=self.name, payload_snippet=repr(refine)[:300],
            )
        jobs = data.get("jobs")
        if jobs is None:
            return []
        if not isinstance(jobs, list):
            raise AdapterError(
                f"expected 'jobs' to be a list, got {type(jobs).__name__}",
                adapter=self.name, payload_snippet=repr(jobs)[:200],
            )
        return jobs

    def _map(self, job: Any, base: str) -> dict[str, Any]:
        if not isinstance(job, dict):
            raise AdapterError(
                f"expected jobs entries to be objects, got {type(job).__name__}",
                adapter=self.name, payload_snippet=repr(job)[:200],
            )

        apply_url = job.get("applyUrl") or job.get("jobUrl")
        if not apply_url:
            raise AdapterError(
                "job has no applyUrl -- cannot build an apply URL",
                adapter=self.name, payload_snippet=repr(job)[:300],
            )

        return {
            "req_id": job.get("jobId") or job.get("reqId"),
            "title": job.get("title"),
            "url": absolute_url(base, str(apply_url)),
            "locations": _locations(job),
            "posted_at": job.get("postedDate"),
            "department": job.get("category") or job.get("department"),
        }


def _locations(job: dict[str, Any]) -> list[str]:
    """Prefer the human-readable pair, fall back to the parts.

    `multi_location` is a list on tenants that post one req to several sites;
    where it exists it is the complete answer and the scalars repeat its first
    entry.
    """
    multi = job.get("multi_location")
    if isinstance(multi, list) and multi:
        return [str(x) for x in multi if x]

    for key in ("cityState", "location", "address"):
        value = job.get(key)
        if value:
            return [str(value)]

    parts = [job.get("city"), job.get("state"), job.get("country")]
    joined = ", ".join(str(p) for p in parts if p)
    return [joined] if joined else []
