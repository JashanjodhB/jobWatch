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
    # `eu.` is optional: EU-hosted boards serve their careers page from
    # job-boards.eu.greenhouse.io but are still read from the ordinary
    # boards-api.greenhouse.io, so only the URL shape differs.
    (re.compile(r"boards\.(?:eu\.)?greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
     "greenhouse", "board_token"),
    (re.compile(r"job-boards\.(?:eu\.)?greenhouse\.io/([\w-]+)", re.I),
     "greenhouse", "board_token"),
    (re.compile(r"jobs\.lever\.co/([\w-]+)", re.I), "lever", "company"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I), "ashby", "board_name"),
    (re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)", re.I), "smartrecruiters", "company"),
    (re.compile(r"careers\.smartrecruiters\.com/([\w-]+)", re.I), "smartrecruiters", "company"),
    # The account is the first path segment; everything after it is the job
    # (`/j/<shortcode>/apply`), so the match must stop at the next slash.
    (re.compile(r"apply\.workable\.com/([\w-]+)", re.I), "workable", "account"),
    (re.compile(r"ats\.rippling\.com/([\w-]+)", re.I), "rippling", "board"),
    (re.compile(r"https?://([\w-]+)\.bamboohr\.com", re.I), "bamboohr", "tenant"),
]

_WORKDAY_URL = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:([\w-]{2,5}-[\w-]{2,5})/)?([\w-]+)", re.I
)

# Workday's *other* domain. Same CXS API, but the tenant moves out of the
# hostname and into the path, so `_WORKDAY_URL` cannot see it:
#     https://wd5.myworkdaysite.com/[en-US/]recruiting/{tenant}/{site}
# Missing this is why Wells Fargo, Microchip, Sysco and Devon Energy all fell
# through to the low-confidence greenhouse guess and 404'd.
_WORKDAY_SITE_URL = re.compile(
    r"https?://(wd\d+)\.myworkdaysite\.com/(?:([\w-]{2,5}-[\w-]{2,5})/)?recruiting/([\w-]+)/([\w-]+)",
    re.I,
)

# Oracle Recruiting Cloud puts the site number in the careers path. The pod that
# actually serves the API is a separate, opaque host and is NOT resolvable from
# here -- the redirect off the company domain lands on an error page, not the
# API -- so this reports the platform and leaves the pod to be filled in.
_ORACLE_SITE = re.compile(r"/sites/(CX_\d+)", re.I)

# Raw iCIMS portals (`careers-<tenant>.icims.com`). Distinct from the Jibe
# front end the `jibe` adapter handles: these have no `/api/jobs`, and every
# path — real or invented — answers 405 behind an AWS WAF human-verification
# challenge, so no plain-HTTP adapter can reach them. Playwright clears the
# challenge, but the listings render in an `in_iframe=1` child frame, which is
# what `browser`'s `frame_contains` exists for.
_ICIMS_HOST = re.compile(r"https?://([\w-]+)\.icims\.com", re.I)

#: Second-level domains that name the ATS rather than the employer. The
#: last-resort board-token guess must refuse these — see `_from_page`.
_ATS_VENDORS = frozenset({
    "greenhouse", "lever", "ashbyhq", "smartrecruiters", "workday",
    "myworkdayjobs", "myworkdaysite", "icims", "workable", "rippling",
    "bamboohr", "jazzhr", "applytojob", "jobvite", "taleo", "avature",
    "successfactors", "eightfold", "phenompeople", "breezy", "recruitee",
    "teamtailor", "paylocity", "oraclecloud", "gr8people", "dayforcehcm",
})

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
    url: str,
    client: httpx.AsyncClient,
    *,
    verify: bool = True,
    max_pages: int | None = None,
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
            await _verify(candidate, client, max_pages=max_pages)

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

    wdsite = _WORKDAY_SITE_URL.search(url)
    if wdsite:
        wd, _locale, tenant, site = wdsite.groups()
        out.append(
            Candidate(
                adapter="workday",
                config={
                    "host": f"https://{wd}.myworkdaysite.com",
                    "tenant": tenant,
                    "site": site,
                    "search_text": "intern",
                },
                confidence="high",
                evidence="tenant and site read off a myworkdaysite.com careers URL",
            )
        )

    icims = _ICIMS_HOST.search(url)
    if icims:
        out.append(_icims_candidate(icims.group(1)))

    oracle = _ORACLE_SITE.search(url)
    if oracle:
        out.append(_oracle_candidate(url, oracle.group(1)))

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

    oracle = _ORACLE_SITE.search(html)
    if oracle:
        out.append(_oracle_candidate(page_url, oracle.group(1)))

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
        if guess in _ATS_VENDORS:
            # The domain names the ATS, not the employer. Guessing here produces
            # a board token that *verifies* — `board_token: greenhouse` returns
            # Greenhouse's own 18 openings — and then alerts on the wrong
            # company's jobs forever. A wrong answer that passes verification is
            # worse than no answer.
            return out
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


def _icims_candidate(tenant: str) -> Candidate:
    """A raw iCIMS portal, reachable only through the browser adapter.

    No `location_selector` on purpose. The right-hand header cell holds the
    location on some tenants and the posting date on others (Daktronics vs
    Peraton), and a date string parsed as a location is worse than no location
    at all: `location_require` rejects a posting that *has* locations and
    matches none of them, so it would silently drop genuine US roles. With the
    field absent the posting carries no location and is never rejected on it.
    """
    host = f"https://{tenant}.icims.com"
    return Candidate(
        adapter="browser",
        config={
            "url": f"{host}/jobs/search?ss=1&searchKeyword=intern",
            "frame_contains": "in_iframe=1",
            "base_url": host,
            "job_selector": "li.iCIMS_JobCardItem",
            "title_selector": "div.title h3",
            "link_selector": "div.title a",
            "wait_ms": 4000,
        },
        confidence="high",
        evidence=f"raw iCIMS portal ({tenant}.icims.com) — WAF-gated, read via the job iframe",
    )


def _oracle_candidate(url: str, site_number: str) -> Candidate:
    """An Oracle tenant.

    The pod host normally cannot be discovered: `careers.<company>.com` does 302
    to an Oracle host, but the redirect lands on
    `/hcmUI/CandidateExperience/errors/404` rather than the API, so following it
    programmatically yields nothing.

    But when the careers URL *is already* on an Oracle host, there is nothing to
    discover — that host is the pod. Community feeds hand these over constantly
    (`egup.fa.us2.oraclecloud.com`, `fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com`),
    and stubbing the pod out for them meant 32 verifiable tenants reported as
    unresolvable. Verified against Vertiv, BNY, Nokia and Tradeweb.
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if parsed.netloc.endswith("oraclecloud.com"):
        return Candidate(
            adapter="oracle",
            config={
                "pod": origin,
                "site_number": site_number,
                "keyword": "intern",
                "limit": 200,
            },
            confidence="high",
            evidence=(
                f"Oracle Recruiting Cloud site {site_number} on an Oracle host — "
                "the careers URL is already the pod"
            ),
        )

    return Candidate(
        adapter="oracle",
        config={
            "pod": "<follow https://HOST/hcmRestApi/... and copy the oraclecloud.com host>",
            "site_number": site_number,
            "site_url": f"{origin}/en/sites/{site_number}",
            "keyword": "intern",
            "limit": 200,
        },
        confidence="medium",
        evidence=(
            f"Oracle Recruiting Cloud site {site_number} in the careers URL — "
            "set `pod` by hand: request /hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions on this host and read the "
            "oraclecloud.com host out of the 302"
        ),
        error="cannot verify until `pod` is filled in",
    )


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


async def _verify(
    candidate: Candidate, client: httpx.AsyncClient, *, max_pages: int | None = None
) -> None:
    """Actually call the candidate endpoint. A guess you have to check is not useful.

    `max_pages` bounds the call for bulk callers: `posting_count` then means "at
    least this many", which is all discovery needs. It is deliberately not the
    default -- a single interactive `jobwatch discover` should report the real
    board size, because that number is how you sanity-check the handle.
    """
    # A candidate can arrive already knowing it is not callable -- Oracle needs a
    # pod that discovery cannot resolve. Calling it anyway would replace a useful
    # message with a protocol error.
    if candidate.error:
        return

    try:
        adapter = get_adapter(candidate.adapter)
    except AdapterError as exc:
        candidate.error = str(exc)
        return

    ctx = FetchContext(
        client=client,
        config=candidate.config,
        adapter_name=candidate.adapter,
        probe=True,
        max_pages=max_pages,
    )
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
