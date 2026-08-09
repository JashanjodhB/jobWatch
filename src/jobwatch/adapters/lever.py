"""Lever postings (§11).

    GET https://api.lever.co/v0/postings/{company}?mode=json

Returns a bare JSON array. `createdAt` is a real epoch-millis timestamp, and per
§13.3 we still do not use it to decide newness -- our own seen-set is the only
truth. Config: {company: <handle>}.
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

API = "https://api.lever.co/v0/postings/{company}"


@register("lever")
class LeverAdapter:
    name: ClassVar[str] = "lever"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        company = str(ctx.require("company")).strip()
        url = API.format(company=company)

        payload, response = await conditional_json(ctx, f"{url}?mode=json")
        if payload is None:
            return FetchResult(not_modified=True, adapter=self.name)

        # The response is the array itself, not an object wrapping one.
        postings = require_list(payload, None, self.name)
        items = [self._map(p) for p in postings]

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            adapter=self.name,
        )

    def _map(self, posting: Any) -> dict[str, Any]:
        if not isinstance(posting, dict):
            raise AdapterError(
                f"expected posting objects, got {type(posting).__name__}",
                adapter=self.name,
                payload_snippet=repr(posting)[:200],
            )
        categories = posting.get("categories") or {}
        if not isinstance(categories, dict):
            categories = {}

        locations: list[str] = []
        if categories.get("location"):
            locations.append(str(categories["location"]))
        for extra in posting.get("allLocations") or categories.get("allLocations") or []:
            if extra:
                locations.append(str(extra))

        return {
            "req_id": posting.get("id"),
            "title": posting.get("text"),
            "url": posting.get("hostedUrl") or posting.get("applyUrl"),
            "locations": locations,
            "posted_at": _epoch_ms(posting.get("createdAt")),
            "department": categories.get("team") or categories.get("department"),
        }


def _epoch_ms(value: Any) -> str | None:
    from datetime import UTC, datetime

    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None
