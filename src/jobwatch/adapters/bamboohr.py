"""BambooHR hosted careers pages (§11).

    GET https://{tenant}.bamboohr.com/careers/list

One request, no pagination, no key. `meta.totalCount` is the board total and
`result` holds the postings.

**The response carries no URL.** Every other board hands one over; here the
apply link has to be assembled as `https://{tenant}.bamboohr.com/careers/{id}`.
Getting that wrong produces alerts that do not open, which is the most annoying
possible way to fail, so the tenant is required config rather than inferred.

Config: {tenant: <subdomain>}, the host segment on
`{tenant}.bamboohr.com/careers`.
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

API = "https://{tenant}.bamboohr.com/careers/list"
POSTING = "https://{tenant}.bamboohr.com/careers/{id}"


@register("bamboohr")
class BambooHrAdapter:
    name: ClassVar[str] = "bamboohr"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        tenant = str(ctx.require("tenant")).strip().lower()

        payload, response = await conditional_json(ctx, API.format(tenant=tenant))
        if payload is None:
            return FetchResult(not_modified=True, adapter=self.name)

        jobs = require_list(payload, "result", self.name)
        items = [self._map(job, tenant) for job in jobs]

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            adapter=self.name,
        )

    def _map(self, job: Any, tenant: str) -> dict[str, Any]:
        if not isinstance(job, dict):
            raise AdapterError(
                f"expected job objects, got {type(job).__name__}",
                adapter=self.name,
                payload_snippet=repr(job)[:200],
            )

        job_id = job.get("id")
        if job_id in (None, ""):
            raise AdapterError(
                "posting has no id, so its apply URL cannot be built",
                adapter=self.name,
                payload_snippet=repr(job)[:200],
            )

        return {
            "req_id": str(job_id),
            # The title field is `jobOpeningName`, not `title`.
            "title": job.get("jobOpeningName"),
            "url": POSTING.format(tenant=tenant, id=job_id),
            "locations": _locations(job),
            "posted_at": None,
            "department": job.get("departmentLabel") or None,
        }


def _locations(job: dict[str, Any]) -> list[str]:
    """`location` is the real one; `atsLocation` is usually all-nulls."""
    out: list[str] = []
    for key in ("location", "atsLocation"):
        node = job.get(key)
        if not isinstance(node, dict):
            continue
        text = ", ".join(
            str(node[k]) for k in ("city", "state", "province", "country") if node.get(k)
        )
        if text:
            out.append(text)
            break

    if not out and job.get("isRemote"):
        out.append("Remote")
    return out
