#!/usr/bin/env python3
"""
verify_sources.py — test every source in companies.yaml and report what works.

Standalone: needs only httpx and pyyaml. Run this from your DEV LAPTOP before
the first real poll, and again whenever alerts go quiet for a company.

    uv run --with httpx --with pyyaml python verify_sources.py companies.yaml

Or:
    pip install httpx pyyaml
    python verify_sources.py companies.yaml

Options:
    --slug NAME     test one company only
    --timeout N     per-request timeout in seconds (default 20)
    --verbose       print a sample title from each working source

Exit code is 0 if every enabled source responded, 1 otherwise — so this
works as a periodic sanity check in cron if you want it.

NOTE: this deliberately does NOT test `direct.*` or `browser` adapters. Those
are bespoke per company and have to be built by hand from the DevTools recipe
in the architecture spec. They are reported as SKIP.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

try:
    import httpx
    import yaml
except ImportError:
    sys.exit("Missing deps. Run: pip install httpx pyyaml")


UA = "jobwatch-verify/1.0 (personal internship monitor; endpoint check)"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _unicode_marks_ok() -> bool:
    """Windows consoles default to cp1252, which cannot encode the status marks.

    Ask the stream for UTF-8 first; if it refuses, check whether the marks
    survive its encoding. Getting this wrong crashes the whole report on its
    first line — after every endpoint has already been called.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        return True
    except (AttributeError, OSError, ValueError):
        pass
    try:
        "✓✗–".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# Single-character fallbacks, so the columns line up either way.
if _unicode_marks_ok():
    MARK_OK, MARK_FAIL, MARK_SKIP = "✓", "✗", "–"
else:
    MARK_OK, MARK_FAIL, MARK_SKIP = "+", "x", "-"


@dataclass
class Result:
    company: str
    adapter: str
    status: str            # OK | FAIL | SKIP
    detail: str
    count: int | None = None
    sample: str | None = None


# ── adapter probes ────────────────────────────────────────────────────────
# Each returns (count, sample_title). Raise on failure.


async def probe_greenhouse(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    token = cfg["board_token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("unexpected shape: 'jobs' is not a list")
    sample = jobs[0].get("title", "?") if jobs else ""
    return len(jobs), sample


async def probe_lever(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    company = cfg["company"]
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("unexpected shape: response is not a list")
    sample = data[0].get("text", "?") if data else ""
    return len(data), sample


async def probe_ashby(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    board = cfg["board_name"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("unexpected shape: 'jobs' is not a list")
    sample = jobs[0].get("title", "?") if jobs else ""
    return len(jobs), sample


async def probe_workday(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    host = cfg["host"].rstrip("/")
    tenant = cfg["tenant"]
    site = cfg["site"]
    url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    body = {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": cfg.get("search_text", "intern"),
    }
    r = await client.post(
        url,
        json=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype:
        raise ValueError(f"returned {ctype!r}, not JSON — tenant or site is wrong")
    data = r.json()
    postings = data.get("jobPostings", [])
    total = data.get("total", len(postings))
    sample = postings[0].get("title", "?") if postings else ""
    return total, sample


async def probe_smartrecruiters(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    company = cfg["company"]
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    content = data.get("content", [])
    total = data.get("totalFound", len(content))
    sample = content[0].get("name", "?") if content else ""
    return total, sample


async def probe_eightfold(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    base = cfg["base"].rstrip("/")
    url = (
        f"{base}/api/apply/v2/jobs?domain={cfg['domain']}"
        f"&start=0&num=10&query={cfg.get('query', '')}"
    )
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    positions = data.get("positions", [])
    if not isinstance(positions, list):
        raise ValueError("unexpected shape: 'positions' is not a list")
    sample = positions[0].get("name", "?") if positions else ""
    return data.get("count", len(positions)), sample


async def probe_html(client: httpx.AsyncClient, cfg: dict) -> tuple[int, str]:
    """Only checks the page loads. Selector validity needs selectolax."""
    r = await client.get(cfg["url"])
    r.raise_for_status()
    return -1, f"page loaded, {len(r.text) // 1024}KB"


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "ashby": probe_ashby,
    "workday": probe_workday,
    "smartrecruiters": probe_smartrecruiters,
    "eightfold": probe_eightfold,
    "html": probe_html,
}


# ── runner ────────────────────────────────────────────────────────────────


async def check_source(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    company: str,
    adapter: str,
    cfg: dict,
) -> Result:
    if adapter.startswith("direct.") or adapter == "browser":
        return Result(company, adapter, "SKIP", "bespoke adapter — build by hand")

    probe = PROBES.get(adapter)
    if probe is None:
        return Result(company, adapter, "SKIP", "no probe implemented")

    async with sem:
        try:
            count, sample = await probe(client, cfg)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            hint = {
                404: "board token / tenant / site is wrong",
                403: "blocked — likely needs the browser fallback",
                401: "requires auth — should not be in this registry",
            }.get(code, "")
            return Result(company, adapter, "FAIL", f"HTTP {code} {hint}".strip())
        except httpx.TimeoutException:
            return Result(company, adapter, "FAIL", "timeout")
        except (KeyError, ValueError, TypeError) as e:
            return Result(company, adapter, "FAIL", f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - report anything unexpected
            return Result(company, adapter, "FAIL", f"{type(e).__name__}: {e}")

    if count == 0:
        return Result(
            company, adapter, "FAIL",
            "responded but returned 0 postings — token probably wrong", 0,
        )
    return Result(company, adapter, "OK", "", count, sample)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="path to companies.yaml")
    ap.add_argument("--slug", help="test one company only")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # Explicit encoding: the registry has box-drawing characters in its comments,
    # and Windows would otherwise open it as cp1252 and choke.
    with open(args.config, encoding="utf-8") as f:
        companies = yaml.safe_load(f)

    if args.slug:
        companies = [c for c in companies if c["slug"] == args.slug]
        if not companies:
            sys.exit(f"No company with slug {args.slug!r}")

    tasks_spec: list[tuple[str, str, dict]] = []
    disabled: list[Result] = []
    for company in companies:
        for src in company.get("sources", []):
            # Companies shipped disabled are deliberate parking spots (they need
            # the browser fallback, or their handle still has to be found).
            # Probing them would report failures that are not news.
            if company.get("enabled", True) is False or src.get("enabled", True) is False:
                disabled.append(
                    Result(company["slug"], src["adapter"], "SKIP", "disabled in the registry")
                )
                continue
            tasks_spec.append((company["slug"], src["adapter"], src.get("config", {})))

    sem = asyncio.Semaphore(8)
    limits = httpx.Limits(max_connections=10)
    async with httpx.AsyncClient(
        timeout=args.timeout,
        headers={"User-Agent": UA, "Accept": "application/json"},
        follow_redirects=True,
        limits=limits,
    ) as client:
        results = list(
            await asyncio.gather(
                *(check_source(client, sem, c, a, cfg) for c, a, cfg in tasks_spec)
            )
        ) + disabled

    ok = [r for r in results if r.status == "OK"]
    fail = [r for r in results if r.status == "FAIL"]
    skip = [r for r in results if r.status == "SKIP"]

    print(f"\n{BOLD}Source verification{RESET}  {len(companies)} companies, "
          f"{len(results)} sources\n")

    for r in sorted(results, key=lambda x: (x.status != "FAIL", x.company)):
        if r.status == "OK":
            mark, colour = MARK_OK, GREEN
            note = f"{r.count} postings"
            if args.verbose and r.sample:
                note += f'  {DIM}"{r.sample[:52]}"{RESET}'
        elif r.status == "FAIL":
            mark, colour = MARK_FAIL, RED
            note = r.detail
        else:
            mark, colour = MARK_SKIP, YELLOW
            note = r.detail
        print(f"  {colour}{mark}{RESET} {r.company:<24} {DIM}{r.adapter:<16}{RESET} {note}")

    print(f"\n  {GREEN}{len(ok)} working{RESET}   "
          f"{RED}{len(fail)} broken{RESET}   "
          f"{YELLOW}{len(skip)} to build by hand{RESET}\n")

    if fail:
        print(f"{BOLD}Fix the broken ones before Phase 1.{RESET}")
        print("Open the careers page, DevTools → Network → Fetch/XHR, search for")
        print("a job, and read the real token/tenant off the request URL.\n")

    if skip:
        print(f"{BOLD}The skipped ones are direct adapters.{RESET}")
        print("Same DevTools recipe, but you write a module per company.")
        print("Start with amazon and apple — they are the most tractable.\n")

    return 1 if fail else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
