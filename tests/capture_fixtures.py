#!/usr/bin/env python3
"""Capture real responses into tests/fixtures/ (§15).

Every adapter is tested against a captured real response rather than a live
call, which is what lets the suite run offline and lets a schema drift show up
as a failing test instead of a silent empty board.

    uv run python tests/capture_fixtures.py [name ...]

Responses are trimmed to a handful of postings each — enough to exercise the
mapping, small enough to read in a diff.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
UA = {
    "User-Agent": "jobwatch/2.0 (personal internship monitor)",
    "Accept": "application/json",
}
APPLE_FORMAT = {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}


def trim(payload, key: str | None, n: int = 4):
    """Keep the envelope, keep only the first n items of the postings array."""
    if key is None:
        return payload[:n] if isinstance(payload, list) else payload
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        payload[key] = payload[key][:n]
    return payload


async def main(only: set[str]) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    async with httpx.AsyncClient(timeout=40, headers=UA, follow_redirects=True) as c:
        jobs: list[tuple[str, str, object]] = [
            ("greenhouse", "get", ("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs", "jobs")),
            ("lever", "get", ("https://api.lever.co/v0/postings/palantir?mode=json", None)),
            ("ashby", "get", ("https://api.ashbyhq.com/posting-api/job-board/openai", "jobs")),
            ("smartrecruiters", "get", ("https://api.smartrecruiters.com/v1/companies/Visa/postings?limit=4", "content")),
            ("eightfold_netflix", "get", ("https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&start=0&num=4&query=intern", "positions")),
            ("direct_amazon", "get", ("https://www.amazon.jobs/en/search.json?base_query=intern&result_limit=4&offset=0&sort=recent", "jobs")),
            ("direct_uber", "post", ("https://www.uber.com/api/loadSearchJobsResults?localeCode=en",
                                      {"params": {"query": "intern"}, "page": 0, "limit": 4},
                                      {"x-csrf-token": "x", "Content-Type": "application/json"}, None)),
            ("direct_apple", "post", ("https://jobs.apple.com/api/v1/search",
                                       {"query": "internship", "filters": {}, "page": 1,
                                        "locale": "en-us", "sort": "newest", "format": APPLE_FORMAT},
                                       {"Content-Type": "application/json",
                                        "Referer": "https://jobs.apple.com/en-us/search"}, None)),
        ]

        for tenant, host, site in [
            ("nvidia", "https://nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite"),
            ("salesforce", "https://salesforce.wd12.myworkdayjobs.com", "External_Career_Site"),
            ("adobe", "https://adobe.wd5.myworkdayjobs.com", "external_experienced"),
        ]:
            jobs.append(
                (
                    f"workday_{tenant}", "post",
                    (f"{host}/wday/cxs/{tenant}/{site}/jobs",
                     {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "intern"},
                     {"Accept": "application/json", "Content-Type": "application/json"},
                     "jobPostings"),
                )
            )

        for name, method, spec in jobs:
            if only and name not in only:
                continue
            try:
                if method == "get":
                    url, key = spec  # type: ignore[misc]
                    r = await c.get(url)
                else:
                    url, body, headers, key = spec  # type: ignore[misc]
                    r = await c.post(url, json=body, headers=headers)
                r.raise_for_status()
                payload = trim(r.json(), key)
            except Exception as exc:
                print(f"  SKIP {name}: {type(exc).__name__}: {str(exc)[:90]}")
                continue

            path = FIXTURES / f"{name}.json"
            path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
            written.append(f"{name} ({path.stat().st_size // 1024}KB)")
            print(f"  ok   {name}")

    print(f"\n{len(written)} fixtures written to {FIXTURES}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(set(sys.argv[1:]))))
