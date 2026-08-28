"""Rippling ATS public boards (§11).

    GET https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs

The response is a **bare JSON array**, not an object with a `jobs` key, so the
shape guard runs against the top level. One request, no pagination, no key.

`ats.rippling.com/api/v1/board/{board}/jobs` serves the identical payload; the
api.rippling.com host is used here because it is the one the board's own page
calls.

Config: {board: <handle>}, the path segment on `ats.rippling.com/{board}`.
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

API = "https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs"


@register("rippling")
class RipplingAdapter:
    name: ClassVar[str] = "rippling"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        board = str(ctx.require("board")).strip()

        payload, response = await conditional_json(ctx, API.format(board=board))
        if payload is None:
            return FetchResult(not_modified=True, adapter=self.name)

        jobs = require_list(payload, None, self.name)
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
            "req_id": job.get("uuid"),
            # The title field is `name`, not `title`.
            "title": job.get("name"),
            "url": job.get("url"),
            "locations": _locations(job),
            # The board exposes no posting date at all; first_seen_at covers it.
            "posted_at": None,
            "department": _label(job.get("department")),
        }


def _label(node: Any) -> str | None:
    """`department` and `workLocation` are {id, label} objects, not strings."""
    if isinstance(node, dict):
        value = node.get("label") or node.get("id")
        return str(value) if value else None
    return str(node) if node else None


def _locations(job: dict[str, Any]) -> list[str]:
    label = _label(job.get("workLocation"))
    return [label] if label else []
