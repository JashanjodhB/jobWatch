"""Bulk company discovery from community internship feeds (§12).

`discover.py` answers "what ATS is behind this one careers URL". This module
answers the question that comes before it: **which companies should I be
watching that I am not?**

The feed is a source of *company names and careers URLs only*. Its postings are
never routed into the pipeline, and that is a correctness requirement rather
than a preference: `pipeline.py` derives both `merge_key` and `dedup_key` from
the source's `company_slug`, so a multi-employer aggregator would collapse
distinct employers into a single alert. What leaves this module is a verified
endpoint on the company's *own* ATS, which is what gets polled.

Cost control matters more here than anywhere else in the system, because this is
the one operation that talks to hundreds of unfamiliar hosts in a single run
from an IP that cannot be rotated (§13.5):

* Feed listings carry the direct ATS URL, so most companies are fingerprinted by
  `_from_url` with **zero** page fetches.
* Verification is bounded to one page (`PROBE_MAX_PAGES`), so confirming a large
  Workday tenant costs one request instead of forty-six.
* Every probed company is written to a ledger, so a weekly run only spends
  requests on companies that are genuinely new.
* `--limit` caps the work per run, and the default is deliberately small.

Nothing here writes to `companies.yaml`. Output is a staging file in the same
shape, to be reviewed and merged by hand — the same convention
`companies-expansion.yaml` already follows.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .db import utcnow
from .discover import Candidate, discover
from .logging_setup import get_logger

__all__ = [
    "DEFAULT_FEEDS",
    "FeedCompany",
    "FeedListing",
    "Ledger",
    "ProbeOutcome",
    "bulk_discover",
    "collect_companies",
    "fetch_feed",
    "is_known",
    "parse_feed",
    "render_yaml",
    "slugify",
]

log = get_logger(__name__)

# Community-maintained CS internship trackers. Both publish a plain JSON array
# and are fetched from raw.githubusercontent.com, so no scraping is involved.
# Their schemas differ slightly; `parse_feed` normalises both.
DEFAULT_FEEDS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
    "/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
    "/dev/.github/scripts/listings.json",
)

# One page is enough to answer "does this endpoint respond with postings".
PROBE_MAX_PAGES = 1

# Aggregator hosts that identify the tracker, not the employer. A URL on one of
# these tells us nothing about the ATS, so it is never used as a careers URL.
_AGGREGATOR_HOSTS = re.compile(
    r"(simplify\.jobs|linkedin\.com|indeed\.com|glassdoor\.|ziprecruiter\.|"
    r"builtin\.com|wellfound\.com|angel\.co|dice\.com|monster\.com)",
    re.I,
)

# Hosts that `discover._from_url` can fingerprint without fetching anything.
_FINGERPRINTABLE = re.compile(
    r"(myworkdayjobs\.com|greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com)",
    re.I,
)


def slugify(name: str) -> str:
    """Company name -> registry slug, matching the convention in companies.yaml."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _root_url(url: str) -> str:
    """The ATS URL with the posting-specific tail removed.

    `discover` fingerprints the host and the leading path segments, so trimming
    at `/job/` keeps everything it needs while producing a careers URL that
    stays valid after the individual req closes.
    """
    for marker in ("/job/", "/jobs/", "/posting/", "/apply/"):
        cut = url.find(marker)
        if cut > 0:
            return url[:cut]
    return url


# ── feed parsing ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class FeedListing:
    """One posting from a feed, reduced to the fields discovery cares about."""

    company_name: str
    title: str
    url: str
    terms: tuple[str, ...]
    category: str
    active: bool


def parse_feed(payload: Any) -> list[FeedListing]:
    """Normalise a feed payload into listings.

    The two default feeds disagree on shape — one carries `terms` as a list, the
    other a single `season` string, and only one has `category`. Both are
    accepted so that adding a feed does not mean changing code.
    """
    if not isinstance(payload, list):
        raise ValueError("feed payload was not a JSON array of listings")

    out: list[FeedListing] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = (row.get("company_name") or "").strip()
        url = (row.get("url") or "").strip()
        if not name or not url:
            continue

        raw_terms = row.get("terms")
        if raw_terms is None:
            season = row.get("season")
            raw_terms = [season] if season else []
        if isinstance(raw_terms, str):
            raw_terms = [raw_terms]

        out.append(
            FeedListing(
                company_name=name,
                title=(row.get("title") or "").strip(),
                url=url,
                terms=tuple(str(t) for t in raw_terms if t),
                category=str(row.get("category") or ""),
                active=bool(row.get("active", True)),
            )
        )
    return out


async def fetch_feed(url: str, client: httpx.AsyncClient) -> list[FeedListing]:
    response = await client.get(url, headers={"Accept": "application/json"}, timeout=60.0)
    response.raise_for_status()
    return parse_feed(response.json())


# ── company aggregation ───────────────────────────────────────────────────


@dataclass(slots=True)
class FeedCompany:
    slug: str
    display_name: str
    careers_url: str = ""
    listings: int = 0
    sample_title: str = ""
    terms: set[str] = field(default_factory=set)
    # True when the careers URL alone identifies the ATS, so probing it costs no
    # page fetch. Used to order a run: cheap, high-confidence work first.
    fingerprintable: bool = False


def collect_companies(
    listings: Iterable[FeedListing],
    *,
    terms: Sequence[str] = (),
    categories: Sequence[str] = (),
    include_inactive: bool = False,
) -> dict[str, FeedCompany]:
    """Group listings by company, keeping the best careers URL for each.

    "Best" means a URL that names the ATS. A company whose listings all point at
    an aggregator is still returned — with that URL dropped — because the name
    is worth reporting even though it cannot be probed automatically.
    """
    want_terms = {t.lower() for t in terms}
    want_cats = {c.lower() for c in categories}
    companies: dict[str, FeedCompany] = {}

    for item in listings:
        if not include_inactive and not item.active:
            continue
        if want_terms and not any(t.lower() in want_terms for t in item.terms):
            continue
        if want_cats and item.category.lower() not in want_cats:
            continue

        slug = slugify(item.company_name)
        if not slug:
            continue

        entry = companies.get(slug)
        if entry is None:
            entry = FeedCompany(
                slug=slug, display_name=item.company_name, sample_title=item.title
            )
            companies[slug] = entry

        entry.listings += 1
        entry.terms.update(item.terms)
        if not entry.sample_title:
            entry.sample_title = item.title

        if _AGGREGATOR_HOSTS.search(item.url):
            continue
        # Prefer a URL the fingerprinter can read; otherwise keep the first
        # non-aggregator URL seen.
        if _FINGERPRINTABLE.search(item.url):
            if not entry.fingerprintable:
                entry.careers_url = _root_url(item.url)
                entry.fingerprintable = True
        elif not entry.careers_url:
            entry.careers_url = _root_url(item.url)

    return companies


def is_known(slug: str, known: set[str]) -> bool:
    """Slug membership, tolerant of the registry's shorter naming.

    The feed says "AQR Capital Management" where companies.yaml says
    `aqr-capital`. Treating those as different is how a registry grows
    duplicates, so a prefix match on either side counts as known.
    """
    if slug in known:
        return True
    return any(slug.startswith(k + "-") or k.startswith(slug + "-") for k in known)


# ── ledger ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Ledger:
    """Which companies have been probed, and how it went.

    Without this, every run re-probes the same few hundred companies that have
    no usable endpoint. With it, a run spends requests only on companies the
    feed has not offered before.
    """

    path: Path
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        # A corrupt ledger costs one wasted run, never the command itself.
        except (OSError, ValueError) as exc:
            log.warning("discovery ledger unreadable, starting fresh: %s", exc)
            return cls(path=path)
        entries = raw.get("companies") if isinstance(raw, dict) else None
        return cls(path=path, entries=entries if isinstance(entries, dict) else {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utcnow(),
            "companies": self.entries,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def status(self, slug: str) -> str:
        entry = self.entries.get(slug)
        return str(entry.get("status", "")) if entry else ""

    def record(self, slug: str, status: str, **extra: Any) -> None:
        entry = self.entries.setdefault(slug, {})
        entry["status"] = status
        entry["last_probed"] = utcnow()
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry.update({k: v for k, v in extra.items() if v is not None})


# ── probing ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ProbeOutcome:
    company: FeedCompany
    candidate: Candidate | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.candidate is not None and self.candidate.verified


async def bulk_discover(
    companies: Sequence[FeedCompany],
    client: httpx.AsyncClient,
    *,
    concurrency: int = 6,
    max_pages: int | None = PROBE_MAX_PAGES,
) -> list[ProbeOutcome]:
    """Probe each company's careers URL, bounded in both breadth and depth."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def probe(company: FeedCompany) -> ProbeOutcome:
        if not company.careers_url:
            return ProbeOutcome(company, error="no non-aggregator URL in the feed")
        async with semaphore:
            try:
                result = await discover(company.careers_url, client, max_pages=max_pages)
            # One unreachable host must not end a run of several hundred.
            except Exception as exc:
                return ProbeOutcome(company, error=f"{type(exc).__name__}: {exc}")
        best = result.best
        if best is None or not best.verified:
            return ProbeOutcome(
                company,
                candidate=best,
                error=(best.error if best else None) or result.page_error or "nothing verified",
            )
        return ProbeOutcome(company, candidate=best)

    return await asyncio.gather(*(probe(c) for c in companies))


def render_yaml(outcomes: Sequence[ProbeOutcome], *, tier: str = "warm") -> str:
    """Registry blocks for everything that verified, in companies.yaml shape."""
    verified = sorted(
        (o for o in outcomes if o.ok), key=lambda o: (-o.company.listings, o.company.slug)
    )
    header = (
        "# jobwatch — bulk-discovered candidates\n"
        "#\n"
        "# Generated by `jobwatch discover-bulk` on "
        f"{utcnow()[:10]}. Every block below was\n"
        "# probed against its live endpoint and returned postings.\n"
        "#\n"
        "# This file is NOT loaded by anything. Review these, then move the ones you\n"
        "# want into config/companies.yaml and re-run `jobwatch seed`.\n"
        "#\n"
        "# Posting counts are a floor, not a total: verification stops after one\n"
        "# page. Re-check anything surprising with `jobwatch discover <url>`.\n"
    )
    if not verified:
        return header + "\n# (nothing verified in this run)\n"

    blocks: list[str] = []
    for outcome in verified:
        company = outcome.company
        candidate = outcome.candidate
        assert candidate is not None  # guaranteed by .ok
        block = candidate.as_yaml_block(company.slug, company.display_name, company.careers_url)
        if tier != "warm":
            block = block.replace("  tier: warm\n", f"  tier: {tier}\n", 1)
        terms = ", ".join(sorted(company.terms)) or "unknown"
        note = f"  # feed: {company.listings} active listing(s) - {terms}\n"
        if company.sample_title:
            note += f'  # e.g. "{company.sample_title[:70]}"\n'
        blocks.append(block + note)
    return header + "\n" + "\n".join(blocks)
