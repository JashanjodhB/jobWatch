"""Careers URL → candidate source config (§12 CLI, §10 Companies screen).

Two passes. First the URL itself, because most ATS platforms put the tenant
straight in the hostname. If that says nothing, fetch the page once and look for
the platform's fingerprints in the markup — an embedded board, a script src, an
iframe, an apply link.

Whatever it produces is then **verified** by actually calling the candidate
endpoint, so the UI shows "greenhouse, board_token=stripe, 412 postings" rather
than a guess you have to check by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from .adapters.base import AdapterError, FetchContext, get_adapter
from .logging_setup import get_logger

__all__ = ["Candidate", "DiscoveryResult", "discover"]

log = get_logger(__name__)


@dataclass(slots=True)
class Candidate:
    adapter: str
    config: dict[str, Any]
    confidence: str          # 'high' | 'medium' | 'low'
    evidence: str
    verified: bool = False
    posting_count: int | None = None
    sample_title: str | None = None
    error: str | None = None

    def as_yaml_block(self, slug: str, display_name: str, careers_url: str) -> str:
        config_items = "\n".join(f"        {k}: {v}" for k, v in self.config.items()) or "        {}"
        return (
            f"- slug: {slug}\n"
            f"  display_name: {display_name}\n"
            f"  tier: warm\n"
            f"  careers_url: {careers_url}\n"
            f"  sources:\n"
            f"    - adapter: {self.adapter}\n"
            f"      priority: 1\n"
            f"      config:\n{config_items}\n"
        )


@dataclass(slots=True)
class DiscoveryResult:
    url: str
    candidates: list[Candidate] = field(default_factory=list)
    page_error: str | None = None

    @property
    def best(self) -> Candidate | None:
        if not self.candidates:
            return None
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            self.candidates,
            key=lambda c: (not c.verified, order.get(c.confidence, 9)),
        )[0]


# ── URL fingerprints ──────────────────────────────────────────────────────

_URL_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
     "greenhouse", "board_token"),
    (re.compile(r"job-boards\.greenhouse\.io/([\w-]+)", re.I), "greenhouse", "board_token"),
    (re.compile(r"jobs\.lever\.co/([\w-]+)", re.I), "lever", "company"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I), "ashby", "board_name"),
    (re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)", re.I), "smartrecruiters", "company"),
    (re.compile(r"careers\.smartrecruiters\.com/([\w-]+)", re.I), "smartrecruiters", "company"),
]

_WORKDAY_URL = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:([\w-]{2,5}-[\w-]{2,5})/)?([\w-]+)", re.I
)

# Eightfold is identified by its API path rather than its hostname: tenants run
# it on their own domains (explore.jobs.netflix.net) as often as on eightfold.ai.
_EIGHTFOLD_API = re.compile(r"/api/apply/v\d/jobs\?domain=([\w.-]+)", re.I)
_EIGHTFOLD_DOMAIN = re.compile(r"[\"'?&]domain[\"']?\s*[:=]\s*[\"']?([\w-]+\.[\w.-]+)", re.I)

# ── page fingerprints ─────────────────────────────────────────────────────

_PAGE_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"boards(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board\?for=)([\w-]+)", re.I),
     "greenhouse", "board_token"),
    (re.compile(r"job-boards\.greenhouse\.io/([\w-]+)", re.I), "greenhouse", "board_token"),
    (re.compile(r"api\.lever\.co/v\d/postings/([\w-]+)", re.I), "lever", "company"),
    (re.compile(r"jobs\.lever\.co/([\w-]+)", re.I), "lever", "company"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([\w.-]+)", re.I), "ashby", "board_name"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I), "ashby", "board_name"),
    (re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w-]+)", re.I),
     "smartrecruiters", "company"),
    (re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)", re.I), "smartrecruiters", "company"),
]


async def discover(
    url: str, client: httpx.AsyncClient, *, verify: bool = True
) -> DiscoveryResult:
    result = DiscoveryResult(url=url)
    seen: set[tuple[str, str]] = set()

    for candidate in _from_url(url):
        marker = (candidate.adapter, repr(sorted(candidate.config.items())))
        if marker not in seen:
            seen.add(marker)
            result.candidates.append(candidate)

    if not result.candidates:
        html = await _fetch_page(url, client, result)
        if html:
            for candidate in _from_page(html, url):
                marker = (candidate.adapter, repr(sorted(candidate.config.items())))
                if marker not in seen:
                    seen.add(marker)
                    result.candidates.append(candidate)

    if not result.candidates:
        # Nothing recognised. An html adapter against the page is still a real
        # option, it just needs a selector chosen by hand.
        result.candidates.append(
            Candidate(
                adapter="html",
                config={"url": url, "job_selector": "a[href*='job']"},
                confidence="low",
                evidence="no ATS fingerprint found — this is a starting point, not an answer",
            )
        )

    if verify:
        for candidate in result.candidates:
            await _verify(candidate, client)

        # Nothing survived verification, so every candidate above is a guess.
        # A client-rendered page (Eightfold, most SPAs) hides its API until the
        # JavaScript runs, so offer the two options that do not depend on
        # fingerprinting rather than leaving a wrong guess as the only answer.
        if not any(c.verified for c in result.candidates):
            result.candidates.append(
                Candidate(
                    adapter="browser",
                    config={"url": url, "xhr_contains": "job"},
                    confidence="low",
                    evidence=(
                        "no endpoint verified — if the page renders its listings "
                        "client-side, open DevTools → Network → Fetch/XHR and put "
                        "the real request substring in xhr_contains"
                    ),
                )
            )

    return result


def _from_url(url: str) -> list[Candidate]:
    out: list[Candidate] = []

    workday = _WORKDAY_URL.search(url)
    if workday:
        tenant, wd, _locale, site = workday.groups()
        out.append(
            Candidate(
                adapter="workday",
                config={
                    "host": f"https://{tenant}.{wd}.myworkdayjobs.com",
                    "tenant": tenant,
                    "site": site,
                    "search_text": "intern",
                },
                confidence="high",
                evidence="tenant, host and site read directly off the careers URL",
            )
        )

    for pattern, adapter, key in _URL_RULES:
        match = pattern.search(url)
        if match:
            out.append(
                Candidate(
                    adapter=adapter,
                    config={key: match.group(1)},
                    confidence="high",
                    evidence=f"{adapter} handle in the careers URL",
                )
            )
    return out


def _from_page(html: str, page_url: str) -> list[Candidate]:
    out: list[Candidate] = []
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"

    eightfold = _EIGHTFOLD_API.search(html) or _EIGHTFOLD_DOMAIN.search(html)
    if eightfold and ("eightfold" in html.lower() or "/api/apply/" in html):
        out.append(
            Candidate(
                adapter="eightfold",
                config={"base": origin, "domain": eightfold.group(1), "query": "intern"},
                confidence="medium",
                evidence="Eightfold apply API referenced in the page markup",
            )
        )

    workday = _WORKDAY_URL.search(html)
    if workday:
        tenant, wd, _locale, site = workday.groups()
        out.append(
            Candidate(
                adapter="workday",
                config={
                    "host": f"https://{tenant}.{wd}.myworkdayjobs.com",
                    "tenant": tenant,
                    "site": site,
                    "search_text": "intern",
                },
                confidence="medium",
                evidence="Workday link found in the page markup",
            )
        )

    for pattern, adapter, key in _PAGE_RULES:
        match = pattern.search(html)
        if match:
            out.append(
                Candidate(
                    adapter=adapter,
                    config={key: match.group(1)},
                    confidence="medium",
                    evidence=f"{adapter} endpoint referenced in the page markup",
                )
            )

    if not out:
        host = urlparse(page_url).netloc.split(".")[-2:]
        guess = host[0] if host else ""
        if guess:
            out.append(
                Candidate(
                    adapter="greenhouse",
                    config={"board_token": guess},
                    confidence="low",
                    evidence=f"guessed board token from the domain name ({guess})",
                )
            )
    return out


async def _fetch_page(url: str, client: httpx.AsyncClient, result: DiscoveryResult) -> str:
    try:
        response = await client.get(
            url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=20.0
        )
        if response.status_code >= 400:
            result.page_error = f"HTTP {response.status_code} fetching the careers page"
            return ""
        return response.text
    except httpx.HTTPError as exc:
        result.page_error = f"{type(exc).__name__}: {exc}"
        return ""


async def _verify(candidate: Candidate, client: httpx.AsyncClient) -> None:
    """Actually call the candidate endpoint. A guess you have to check is not useful."""
    try:
        adapter = get_adapter(candidate.adapter)
    except AdapterError as exc:
        candidate.error = str(exc)
        return

    ctx = FetchContext(client=client, config=candidate.config, adapter_name=candidate.adapter, probe=True)
    try:
        result = await adapter.fetch(ctx)
    # Verification failing is information to show the user, not an error to raise.
    except Exception as exc:
        candidate.error = str(exc)
        return

    candidate.verified = True
    candidate.posting_count = len(result.postings)
    candidate.sample_title = result.postings[0].title if result.postings else None
    if candidate.posting_count == 0:
        candidate.error = "endpoint responded but returned 0 postings — the handle is probably wrong"
        candidate.verified = False
