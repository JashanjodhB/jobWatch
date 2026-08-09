"""Greenhouse job boards — the easiest adapter in the system (§11).

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs

Public, unauthenticated, stable integer IDs, and `absolute_url` is already a
full apply link. Config: {board_token: <token>}.
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

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
EMBED_API = "https://boards-api.greenhouse.io/v1/boards/{token}/embed/jobs"


@register("greenhouse")
class GreenhouseAdapter:
    name: ClassVar[str] = "greenhouse"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        token = str(ctx.require("board_token")).strip()
        url = (EMBED_API if ctx.config.get("embed") else API).format(token=token)

        payload, response = await conditional_json(ctx, url)
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
        return {
            "req_id": job.get("id"),
            "title": job.get("title"),
            "url": job.get("absolute_url"),
            "locations": _locations(job),
            # Greenhouse's updated_at is real, unlike most. Still display-only (§13.3).
            "posted_at": job.get("updated_at") or job.get("first_published"),
            "department": _first_name(job.get("departments")),
        }


def _locations(job: dict[str, Any]) -> list[str]:
    out: list[str] = []
    location = job.get("location")
    if isinstance(location, dict) and location.get("name"):
        out.append(str(location["name"]))
    elif isinstance(location, str) and location:
        out.append(location)

    offices = job.get("offices")
    if isinstance(offices, list):
        out.extend(str(o["name"]) for o in offices if isinstance(o, dict) and o.get("name"))
    return out


def _first_name(nodes: Any) -> str | None:
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("name"):
                return str(node["name"])
    return None
