"""Oracle Recruiting Cloud / Oracle Fusion HCM career sites (§11).

    GET {pod}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site_number},limit=25,offset=0,keyword=intern

Public and unauthenticated, with a stable `Id` and a real `PostedDate` — the two
things a monitor actually needs. Like Eightfold this is a platform rather than
one company's page, so one adapter serves every tenant.

**The pod is not derivable from the company domain.** `careers.<company>.com`
302s to an opaque Oracle host (American Express -> `egug.fa.us2.oraclecloud.com`),
so the redirect has to be followed once by hand and the result stored in config.
Identify a tenant by a careers URL containing `/sites/CX_<n>`.

`expand=` is required: without it the response still reports the correct
`TotalJobsCount` but omits `requisitionList` entirely, which looks exactly like
a board with no open reqs.

Config: {pod: https://egug.fa.us2.oraclecloud.com, site_number: CX_1}
Optional: {site_url, keyword, limit, sort_by}
"""

from __future__ import annotations

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

PAGE_SIZE = 25
MAX_PAGES = 40
API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
EXPAND = "requisitionList.secondaryLocations"


@register("oracle")
class OracleAdapter:
    name: ClassVar[str] = "oracle"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        pod = str(ctx.require("pod")).rstrip("/")
        site_number = str(ctx.require("site_number")).strip()
        keyword = str(ctx.config.get("keyword", ""))
        sort_by = str(ctx.config.get("sort_by", "POSTING_DATES_DESC"))
        page_size = int(ctx.config.get("limit", PAGE_SIZE))
        # Where a human applies. Tenants usually front the pod with their own
        # domain, and that is the URL an alert should link to.
        site_url = str(ctx.config.get("site_url", "")).rstrip("/")

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        total: int | None = None
        etag = last_modified = None

        for page in range(MAX_PAGES):
            finder = (
                f"findReqs;siteNumber={site_number},limit={page_size},offset={offset}"
                f",sortBy={sort_by}"
            )
            if keyword:
                finder += f",keyword={keyword}"
            url = f"{pod}{API_PATH}?onlyData=true&expand={EXPAND}&finder={finder}"

            payload, response = await conditional_json(ctx, url, use_conditional=(page == 0))
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")

            result = self._result_block(payload)
            if page == 0:
                count = result.get("TotalJobsCount")
                if isinstance(count, int):
                    total = count

            requisitions = self._requisitions(result, total)

            new_on_page = 0
            for requisition in requisitions:
                mapped = self._map(requisition, pod, site_number, site_url)
                if mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            offset += len(requisitions)
            if not requisitions or new_on_page == 0:
                break
            if isinstance(total, int) and offset >= total:
                break
            if len(requisitions) < page_size:
                break
            if ctx.max_pages is not None and page + 1 >= ctx.max_pages:
                break
        else:
            raise AdapterError(
                f"pagination exceeded {MAX_PAGES} pages at {site_number}",
                adapter=self.name,
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    def _result_block(self, payload: Any) -> dict[str, Any]:
        """The single search-result object Oracle wraps every response in."""
        entries = require_list(payload, "items", self.name)
        if not entries:
            raise AdapterError(
                "response carried an empty 'items' array -- expected one search result block",
                adapter=self.name,
                payload_snippet=repr(payload)[:300],
            )
        first = entries[0]
        if not isinstance(first, dict):
            raise AdapterError(
                f"expected items[0] to be an object, got {type(first).__name__}",
                adapter=self.name,
                payload_snippet=repr(first)[:300],
            )
        return first

    def _requisitions(self, result: dict[str, Any], total: int | None) -> list[Any]:
        """The postings array, distinguishing an empty board from schema drift.

        `requisitionList` is genuinely absent when a search matches nothing, so
        it cannot be required unconditionally. It *can* be required whenever the
        response also claims a non-zero total -- that combination is drift, and
        drift has to be loud or the source fails silently for months (§13.10).
        """
        requisitions = result.get("requisitionList")
        if requisitions is None:
            if total == 0 or result.get("TotalJobsCount") == 0:
                return []
            raise AdapterError(
                "'requisitionList' missing while TotalJobsCount is "
                f"{result.get('TotalJobsCount')!r} -- expand= may have been dropped",
                adapter=self.name,
                payload_snippet=repr(result)[:300],
            )
        if not isinstance(requisitions, list):
            raise AdapterError(
                f"expected 'requisitionList' to be a list, got {type(requisitions).__name__}",
                adapter=self.name,
                payload_snippet=repr(requisitions)[:300],
            )
        return requisitions

    def _map(
        self, requisition: Any, pod: str, site_number: str, site_url: str
    ) -> dict[str, Any]:
        if not isinstance(requisition, dict):
            raise AdapterError(
                f"expected requisitionList entries to be objects, "
                f"got {type(requisition).__name__}",
                adapter=self.name,
                payload_snippet=repr(requisition)[:200],
            )

        req_id = requisition.get("Id")
        if not req_id:
            raise AdapterError(
                "requisition has no 'Id'",
                adapter=self.name,
                payload_snippet=repr(requisition)[:300],
            )

        if site_url:
            url = f"{site_url}/job/{req_id}"
        else:
            url = f"{pod}/hcmUI/CandidateExperience/en/sites/{site_number}/job/{req_id}"

        return {
            "req_id": str(req_id),
            "title": requisition.get("Title"),
            "url": url,
            "locations": _locations(requisition),
            "posted_at": _date(requisition.get("PostedDate")),
            "department": requisition.get("Department") or requisition.get("JobFamily"),
        }


def _locations(requisition: dict[str, Any]) -> list[str]:
    out: list[str] = []
    primary = requisition.get("PrimaryLocation")
    if primary:
        out.append(_tidy(primary))

    secondary = requisition.get("secondaryLocations")
    if isinstance(secondary, list):
        for entry in secondary:
            if not isinstance(entry, dict):
                continue
            name = entry.get("Name") or entry.get("LocationName")
            if name:
                out.append(_tidy(name))

    seen: set[str] = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def _tidy(value: Any) -> str:
    """'BRIGHTON, EAST SUSSEX, United Kingdom' -> 'Brighton, East Sussex, UK'."""
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    tidied: list[str] = []
    for part in parts:
        # Oracle tenants frequently upper-case the whole string. Title-casing it
        # back would turn the state code in "SUNRISE, FL" into "Fl", so short
        # all-caps tokens -- state and province codes -- are left alone.
        tidied.append(part.title() if part.isupper() and len(part) > 2 else part)
    if tidied and tidied[-1] in ("United States of America", "United States"):
        tidied[-1] = "USA"
    elif tidied and tidied[-1] == "United Kingdom":
        tidied[-1] = "UK"
    return ", ".join(tidied)


def _date(value: Any) -> str | None:
    """Oracle sends a bare date; the pipeline wants an ISO-8601 instant."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text}T00:00:00Z"
    return text
