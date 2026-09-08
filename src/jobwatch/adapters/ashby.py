"""Ashby public job boards (§11).

    GET https://api.ashbyhq.com/posting-api/job-board/{board_name}

Common at AI labs. `isListed: false` marks postings that exist but are not shown
on the public board; those are excluded unless `include_unlisted` is set.

A board can carry a posting with an empty `title` -- phonely has served one
since 2026-08-25 -- and one such row must not cost the other nineteen. It is
dropped, the same way workday drops a row with no `externalPath`; *every* row
being titleless is a renamed field and still raises (§13.10).

Config: {board_name: <board>}.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..logging_setup import get_logger
from ..models import FetchResult
from .base import (
    AdapterError,
    FetchContext,
    build_postings,
    conditional_json,
    register,
    require_list,
)

API = "https://api.ashbyhq.com/posting-api/job-board/{board}"

log = get_logger(__name__)


@register("ashby")
class AshbyAdapter:
    name: ClassVar[str] = "ashby"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        board = str(ctx.require("board_name")).strip()
        url = API.format(board=board)
        include_unlisted = bool(ctx.config.get("include_unlisted", False))

        payload, response = await conditional_json(ctx, url)
        if payload is None:
            return FetchResult(not_modified=True, adapter=self.name)

        jobs = require_list(payload, "jobs", self.name)
        considered = [job for job in jobs if include_unlisted or _is_listed(job)]

        items: list[dict[str, Any]] = []
        untitled = 0
        for job in considered:
            mapped = self._map(job)
            # One row with no title is bad data on the board, not drift, and
            # dropping the whole poll for it loses every other posting.
            if not str(mapped.get("title") or "").strip():
                untitled += 1
                continue
            items.append(mapped)

        # Every row being untitled is what a renamed `title` field looks like,
        # and that has to stay loud.
        if considered and untitled == len(considered):
            raise AdapterError(
                f"every posting on the board has no title ({untitled} rows) "
                "-- the field was renamed",
                adapter=self.name,
                payload_snippet=repr(considered[0])[:300],
            )
        if untitled:
            log.warning(
                "ashby board %s served %d posting(s) with no title; skipped",
                board, untitled,
            )

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

        locations: list[str] = []
        if job.get("location"):
            locations.append(str(job["location"]))
        for extra in job.get("secondaryLocations") or []:
            if isinstance(extra, dict) and extra.get("location"):
                locations.append(str(extra["location"]))
            elif isinstance(extra, str):
                locations.append(extra)
        if job.get("isRemote") and not locations:
            locations.append("Remote")

        return {
            "req_id": job.get("id"),
            "title": job.get("title"),
            "url": job.get("jobUrl") or job.get("applyUrl"),
            "locations": locations,
            "posted_at": job.get("publishedAt") or job.get("updatedAt"),
            "department": job.get("department") or job.get("team"),
        }


def _is_listed(job: Any) -> bool:
    return not isinstance(job, dict) or job.get("isListed", True) is not False
