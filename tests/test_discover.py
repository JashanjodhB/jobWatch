"""Careers URL -> candidate source config.

Covers the Oracle Recruiting Cloud path specifically, because it is the one
platform whose config cannot be fully resolved by discovery: the pod that serves
the API is an opaque host, and the redirect off the company domain lands on an
error page rather than the API. The candidate therefore has to be *reported and
not verified*, which is a different shape from every other adapter here.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import text_client
from jobwatch.discover import discover


def _oracle(result):
    return next((c for c in result.candidates if c.adapter == "oracle"), None)


async def test_oracle_site_number_read_from_the_careers_url():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            "https://careers.americanexpress.com/en/sites/CX_1/jobs", client, verify=False
        )
    finally:
        await client.aclose()

    candidate = _oracle(result)
    assert candidate is not None
    assert candidate.config["site_number"] == "CX_1"
    assert candidate.config["site_url"] == (
        "https://careers.americanexpress.com/en/sites/CX_1"
    )


async def test_oracle_candidate_is_reported_but_never_verified():
    """The pod is not discoverable, so calling the endpoint would only produce
    a protocol error in place of a useful instruction."""
    client = text_client("<html></html>")
    try:
        result = await discover(
            "https://careers.example.com/en/sites/CX_2/jobs", client, verify=True
        )
    finally:
        await client.aclose()

    candidate = _oracle(result)
    assert candidate is not None
    assert candidate.verified is False
    assert "pod" in (candidate.error or "")
    assert "oraclecloud.com" in candidate.evidence


async def test_oracle_detected_from_page_markup():
    page = '<a href="https://careers.example.com/en/sites/CX_7/job/123">Job</a>'
    client = text_client(page)
    try:
        result = await discover("https://example.com/careers", client, verify=False)
    finally:
        await client.aclose()

    candidate = _oracle(result)
    assert candidate is not None
    assert candidate.config["site_number"] == "CX_7"


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/stripe",
        "https://jobs.lever.co/palantir",
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
    ],
)
async def test_other_platforms_do_not_produce_an_oracle_candidate(url):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(url, client, verify=False)
    finally:
        await client.aclose()

    assert _oracle(result) is None


async def test_oracle_pod_is_taken_from_an_oracle_careers_host():
    """When the careers URL is already on an Oracle host, that host IS the pod.

    Community feeds hand these over constantly. Stubbing the pod out for them
    reported 32 verifiable tenants as unresolvable.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            "https://egup.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
            client,
            verify=False,
        )
    finally:
        await client.aclose()

    candidate = _oracle(result)
    assert candidate is not None
    assert candidate.config["pod"] == "https://egup.fa.us2.oraclecloud.com"
    assert candidate.config["site_number"] == "CX_1001"
    assert candidate.error is None
    assert candidate.confidence == "high"


@pytest.mark.parametrize(
    "host",
    ["https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com", "https://emit.fa.ca3.oraclecloud.com"],
)
async def test_oracle_pod_covers_both_host_shapes(host):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            f"{host}/hcmUI/CandidateExperience/en/sites/CX_1", client, verify=False
        )
    finally:
        await client.aclose()

    assert _oracle(result).config["pod"] == host


async def test_oracle_off_an_ordinary_company_domain_still_needs_the_manual_step():
    """The fix must not swallow the case it was never able to resolve."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            "https://careers.example.com/en/sites/CX_1/jobs", client, verify=False
        )
    finally:
        await client.aclose()

    candidate = _oracle(result)
    assert candidate.error is not None
    assert candidate.config["pod"].startswith("<")


async def test_workday_read_from_the_myworkdaysite_domain():
    """Workday's other domain moves the tenant out of the host and into the path."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs", client, verify=False
        )
    finally:
        await client.aclose()

    best = result.best
    assert best.adapter == "workday"
    assert best.config["host"] == "https://wd1.myworkdaysite.com"
    assert best.config["tenant"] == "wf"
    assert best.config["site"] == "WellsFargoJobs"


async def test_workday_myworkdaysite_tolerates_a_locale_segment():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            "https://wd5.myworkdaysite.com/en-US/recruiting/devon/DevonCareers",
            client,
            verify=False,
        )
    finally:
        await client.aclose()

    assert result.best.config["tenant"] == "devon"
    assert result.best.config["site"] == "DevonCareers"


async def test_raw_icims_portal_becomes_a_browser_candidate():
    """iCIMS is WAF-gated and renders its jobs in an iframe (§11)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover("https://careers-daktronics.icims.com", client, verify=False)
    finally:
        await client.aclose()

    best = result.best
    assert best.adapter == "browser"
    assert best.config["frame_contains"] == "in_iframe=1"
    assert best.config["job_selector"] == "li.iCIMS_JobCardItem"
    # A date string parsed as a location would get genuine US roles rejected.
    assert "location_selector" not in best.config


@pytest.mark.parametrize(
    "url,adapter,config",
    [
        ("https://apply.workable.com/pony-dot-ai/j/4C1F53EF5D/apply",
         "workable", {"account": "pony-dot-ai"}),
        ("https://ats.rippling.com/spreeai", "rippling", {"board": "spreeai"}),
        ("https://specteraerospace.bamboohr.com/careers/121/",
         "bamboohr", {"tenant": "specteraerospace"}),
    ],
)
async def test_new_platforms_are_read_off_the_url(url, adapter, config):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(url, client, verify=False)
    finally:
        await client.aclose()

    assert result.best.adapter == adapter
    assert result.best.config == config


async def test_greenhouse_eu_boards_keep_their_own_token():
    """EU boards serve their careers page from job-boards.eu.greenhouse.io but
    are still read from the ordinary API. Missing the `eu.` segment sent these
    to the last-resort guess, which produced `board_token: greenhouse`."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover(
            "https://job-boards.eu.greenhouse.io/axiomaticai", client, verify=False
        )
    finally:
        await client.aclose()

    assert result.best.adapter == "greenhouse"
    assert result.best.config == {"board_token": "axiomaticai"}


async def test_board_token_is_never_guessed_from_an_ats_vendor_domain():
    """`board_token: greenhouse` verifies — it returns Greenhouse's own
    openings — and would then alert on the wrong company's jobs forever. A
    wrong answer that passes verification is worse than no answer."""
    from jobwatch.discover import _from_page

    assert _from_page("<html></html>", "https://job-boards.eu.greenhouse.io/x") == []
    assert _from_page("<html></html>", "https://careers-x.icims.com/jobs") == []
    # An ordinary employer domain still gets its low-confidence guess.
    guessed = _from_page("<html></html>", "https://careers.acmecorp.com/jobs")
    assert guessed[0].config == {"board_token": "acmecorp"}
    assert guessed[0].confidence == "low"


async def test_known_platforms_are_still_read_straight_off_the_url():
    """Regression guard: adding Oracle must not disturb the existing rules."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        result = await discover("https://boards.greenhouse.io/stripe", client, verify=False)
    finally:
        await client.aclose()

    best = result.best
    assert best is not None
    assert best.adapter == "greenhouse"
    assert best.config == {"board_token": "stripe"}
