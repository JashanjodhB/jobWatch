"""SmartRecruiters public postings (§11).

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=0

Paginated with offset/limit and a `totalFound`. The public apply URL is built
from the company identifier and posting id, not returned whole.

`q` is a full-text filter over the whole posting, not just the title, and it is
the only way to keep a large employer under MAX_PAGES: Bosch's board is 4791
postings (48 pages) unfiltered but 1259 (13 pages) at `q=intern`, and Eurofins'
2533 becomes 633. Prefer `q: intern` over `q: internship` -- the latter cuts
Eurofins to 22 and drops titles that only ever say "Intern".

Config: {company: <identifier>}. Optional: {q, limit}.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from ..models import FetchResult
from .base import (
    AdapterError,
    FetchContext,
    build_postings,
    conditional_json,
    register,
    require_list,
)

API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
APPLY = "https://jobs.smartrecruiters.com/{company}/{posting_id}"

PAGE_SIZE = 100
MAX_PAGES = 20


@register("smartrecruiters")
class SmartRecruitersAdapter:
    name: ClassVar[str] = "smartrecruiters"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        company = str(ctx.require("company")).strip()
        url = API.format(company=company)
        limit = int(ctx.config.get("limit", PAGE_SIZE))
        query = str(ctx.config.get("q", "")).strip()
        suffix = f"&q={quote(query)}" if query else ""

        items: list[dict[str, Any]] = []
        offset = 0
        etag = last_modified = None

        for page in range(MAX_PAGES):
            page_url = f"{url}?limit={limit}&offset={offset}{suffix}"
            # Conditional headers only make sense on the first page; a 304 on a
            # later page would silently truncate the board.
            payload, response = await conditional_json(
                ctx, page_url, use_conditional=(page == 0)
            )
            if payload is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")

            content = require_list(payload, "content", self.name)
            items.extend(self._map(p, company) for p in content)

            total = payload.get("totalFound") if isinstance(payload, dict) else None
            offset += len(content)
            if not content or (isinstance(total, int) and offset >= total):
                break
            if ctx.max_pages is not None and page + 1 >= ctx.max_pages:
                break
        else:
            raise AdapterError(
                f"pagination exceeded {MAX_PAGES} pages -- refusing to keep hammering"
                + ("" if query else "; set `q` to narrow this board"),
                adapter=self.name,
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    def _map(self, posting: Any, company: str) -> dict[str, Any]:
        if not isinstance(posting, dict):
            raise AdapterError(
                f"expected posting objects, got {type(posting).__name__}",
                adapter=self.name,
                payload_snippet=repr(posting)[:200],
            )

        posting_id = posting.get("id") or posting.get("uuid")
        identifier = (posting.get("company") or {}).get("identifier") or company

        return {
            "req_id": posting.get("refNumber") or posting_id,
            "title": posting.get("name"),
            "url": APPLY.format(company=identifier, posting_id=posting_id),
            "locations": _locations(posting.get("location")),
            "posted_at": posting.get("releasedDate") or posting.get("createdOn"),
            "department": (posting.get("department") or {}).get("label"),
        }


def _locations(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return [str(node)] if node else []
    parts = [node.get("city"), node.get("region"), node.get("country")]
    label = ", ".join(str(p) for p in parts if p)
    out = [label] if label else []
    if node.get("remote"):
        out.append("Remote")
    return out
