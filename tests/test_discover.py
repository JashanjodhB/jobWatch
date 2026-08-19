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
