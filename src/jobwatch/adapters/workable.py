"""Workable public job boards (§11).

    GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true

One request, no pagination, no key. `details=true` is what turns the response
from a bare count into the `jobs` array — without it the board looks empty,
which is the failure §13.10 exists to make loud rather than silent.

There is also a newer `POST /api/v3/accounts/{account}/jobs` that pages through
`results`/`nextPage`. The v1 widget returns the whole board in one call, so it
is preferred; v3 is only worth reaching for if a board outgrows it.

Config: {account: <handle>}. The handle is the path segment on
`apply.workable.com/{account}` — note Workable spells dots out, so pony.ai is
`pony-dot-ai`.
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

API = "https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"


@register("workable")
class WorkableAdapter:
    name: ClassVar[str] = "workable"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        account = str(ctx.require("account")).strip()

        payload, response = await conditional_json(ctx, API.format(account=account))
        if payload is None:
            return FetchResult(not_modified=True, adapter=self.name)

        jobs = require_list(payload, "jobs", self.name)
        items = [self._map(job) for job in jobs]

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            adapter=self.name,
        )

    def _map(self, job: Any) -> dict[str, Any]:
        if not isinstance(job, dict):
            raise AdapterError(
                f"expected job objects, got {type(job).__name__}",
                adapter=self.name,
                payload_snippet=repr(job)[:200],
            )

        locations = _locations(job)
        if job.get("telecommuting") and not locations:
            locations.append("Remote")

        return {
            "req_id": job.get("shortcode") or job.get("code") or None,
            "title": job.get("title"),
            # `url` is the public posting; `application_url` is the same page
            # with /apply appended. Link to the posting so the alert opens on
            # the description rather than a bare form.
            "url": job.get("url") or job.get("shortlink") or job.get("application_url"),
            "locations": locations,
            "posted_at": job.get("published_on") or job.get("created_at"),
            "department": job.get("department") or None,
        }


def _locations(job: dict[str, Any]) -> list[str]:
    """City/state/country, from the `locations` array when present.

    Older boards carry only the flat `city`/`state`/`country` fields, newer ones
    add a `locations` array for multi-site roles; take the array when it is
    there so a two-city posting is not reported as one.
    """
    out: list[str] = []

    for loc in job.get("locations") or []:
        if isinstance(loc, dict):
            text = ", ".join(
                str(loc[k]) for k in ("city", "region", "country") if loc.get(k)
            )
            if text:
                out.append(text)

    if not out:
        text = ", ".join(
            str(job[k]) for k in ("city", "state", "country") if job.get(k)
        )
        if text:
            out.append(text)

    return out
