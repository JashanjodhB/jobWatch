"""Adapter tests against captured real responses (§15).

The schema-drift tests are the point of this file. An adapter that returns `[]`
because a field was renamed looks exactly like a company with no open
internships, and will fail silently for months. Every adapter must raise
`AdapterError` instead (§13.10).
"""

from __future__ import annotations

import copy

import httpx
import pytest

from conftest import json_client, load_fixture, sequence_client, text_client
from jobwatch.adapters.base import AdapterError, FetchContext, get_adapter
from jobwatch.adapters.browser import dig

# adapter name -> (fixture, config, key holding the postings array)
CASES = [
    ("greenhouse", "greenhouse", {"board_token": "anthropic"}, "jobs"),
    ("lever", "lever", {"company": "palantir"}, None),
    ("ashby", "ashby", {"board_name": "openai"}, "jobs"),
    ("smartrecruiters", "smartrecruiters", {"company": "Visa"}, "content"),
    ("eightfold", "eightfold_netflix",
     {"base": "https://explore.jobs.netflix.net", "domain": "netflix.com"}, "positions"),
    ("direct.amazon", "direct_amazon", {"base": "https://www.amazon.jobs"}, "jobs"),
    ("direct.uber", "direct_uber", {"base": "https://www.uber.com"}, None),
    ("workday", "workday_nvidia",
     {"host": "https://nvidia.wd5.myworkdayjobs.com", "tenant": "nvidia",
      "site": "NVIDIAExternalCareerSite"}, "jobPostings"),
]


async def fetch(adapter_name: str, payload, config: dict, **ctx_kw):
    client = json_client(payload)
    try:
        ctx = FetchContext(
            client=client, config=config, adapter_name=adapter_name,
            company_slug="test", **ctx_kw
        )
        return await get_adapter(adapter_name).fetch(ctx)
    finally:
        await client.aclose()


# ── happy path ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("adapter,fixture,config,_key", CASES)
async def test_adapter_parses_its_fixture(adapter, fixture, config, _key):
    result = await fetch(adapter, load_fixture(fixture), config)
    assert result.postings, f"{adapter} returned nothing from its own fixture"
    for post in result.postings:
        assert post.title.strip()
        assert post.url.startswith(("http://", "https://")), f"{adapter}: {post.url!r}"


async def test_apple_parses_its_fixture():
    result = await fetch(
        "direct.apple", load_fixture("direct_apple"), {"base": "https://jobs.apple.com"}
    )
    assert result.postings
    assert all(p.url.startswith("https://jobs.apple.com/en-us/details/") for p in result.postings)


@pytest.mark.parametrize("tenant", ["nvidia", "salesforce", "adobe"])
async def test_workday_fixtures_from_three_tenants(tenant):
    """Phase 8 acceptance: all three Workday fixtures parse."""
    result = await fetch(
        "workday",
        load_fixture(f"workday_{tenant}"),
        {"host": f"https://{tenant}.wd5.myworkdayjobs.com", "tenant": tenant, "site": "Site"},
    )
    assert result.postings
    for post in result.postings:
        assert f"https://{tenant}.wd5.myworkdayjobs.com/en-US/Site/" in post.url
        assert post.req_id


async def test_workday_never_parses_posted_on_as_a_timestamp():
    """§13.3: `postedOn` is human text and is carried through verbatim."""
    result = await fetch(
        "workday", load_fixture("workday_nvidia"),
        {"host": "https://h", "tenant": "t", "site": "s"},
    )
    posted = [p.posted_at for p in result.postings if p.posted_at]
    assert posted and any("Posted" in str(p) for p in posted)


async def test_greenhouse_builds_absolute_apply_urls():
    result = await fetch("greenhouse", load_fixture("greenhouse"), {"board_token": "anthropic"})
    assert all(p.url.startswith("https://") for p in result.postings)


async def test_uber_unescapes_html_entities():
    result = await fetch("direct.uber", load_fixture("direct_uber"), {})
    joined = " ".join(f"{p.title} {p.department or ''}" for p in result.postings)
    assert "&amp;" not in joined


async def test_eightfold_tidies_location_strings():
    result = await fetch(
        "eightfold", load_fixture("eightfold_netflix"),
        {"base": "https://explore.jobs.netflix.net", "domain": "netflix.com"},
    )
    for post in result.postings:
        for loc in post.locations:
            assert "United States of America" not in loc


# ── schema drift: the tests that matter ───────────────────────────────────


@pytest.mark.parametrize("adapter,fixture,config,key", [c for c in CASES if c[3]])
async def test_renamed_postings_array_raises(adapter, fixture, config, key):
    """Rename the postings array and assert AdapterError, never []."""
    mutated = copy.deepcopy(load_fixture(fixture))
    mutated[f"{key}_renamed"] = mutated.pop(key)

    with pytest.raises(AdapterError) as exc:
        await fetch(adapter, mutated, config)
    assert key in str(exc.value)


@pytest.mark.parametrize("adapter,fixture,config,key", [c for c in CASES if c[3]])
async def test_postings_array_becomes_wrong_type_raises(adapter, fixture, config, key):
    mutated = copy.deepcopy(load_fixture(fixture))
    mutated[key] = {"unexpectedly": "an object"}
    with pytest.raises(AdapterError):
        await fetch(adapter, mutated, config)


async def test_workday_missing_external_path_raises():
    mutated = copy.deepcopy(load_fixture("workday_nvidia"))
    del mutated["jobPostings"][0]["externalPath"]
    with pytest.raises(AdapterError, match="externalPath"):
        await fetch("workday", mutated, {"host": "https://h", "tenant": "t", "site": "s"})


async def test_amazon_missing_job_path_raises():
    mutated = copy.deepcopy(load_fixture("direct_amazon"))
    del mutated["jobs"][0]["job_path"]
    with pytest.raises(AdapterError, match="job_path"):
        await fetch("direct.amazon", mutated, {})


async def test_apple_missing_res_envelope_raises():
    with pytest.raises(AdapterError, match="res"):
        await fetch("direct.apple", {"unexpected": True}, {})


async def test_greenhouse_missing_title_raises_with_payload_context():
    mutated = copy.deepcopy(load_fixture("greenhouse"))
    mutated["jobs"][0]["title"] = ""
    with pytest.raises(AdapterError) as exc:
        await fetch("greenhouse", mutated, {"board_token": "x"})
    assert exc.value.payload_snippet


async def test_html_response_where_json_expected_raises():
    client = text_client("<html>not json</html>")
    try:
        ctx = FetchContext(client=client, config={"board_token": "x"}, adapter_name="greenhouse")
        with pytest.raises(AdapterError, match="expected JSON"):
            await get_adapter("greenhouse").fetch(ctx)
    finally:
        await client.aclose()


@pytest.mark.parametrize("status,needle", [(404, "404"), (403, "403"), (401, "auth")])
async def test_http_errors_carry_a_useful_hint(status, needle):
    client = json_client({"error": "nope"}, status=status)
    try:
        ctx = FetchContext(client=client, config={"board_token": "x"}, adapter_name="greenhouse")
        with pytest.raises(AdapterError) as exc:
            await get_adapter("greenhouse").fetch(ctx)
        assert needle in str(exc.value).lower()
    finally:
        await client.aclose()


# ── conditional requests and pagination ───────────────────────────────────


async def test_304_returns_not_modified_without_parsing():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(304))
    )
    try:
        ctx = FetchContext(
            client=client, config={"board_token": "x"}, adapter_name="greenhouse", etag='W/"abc"'
        )
        result = await get_adapter("greenhouse").fetch(ctx)
    finally:
        await client.aclose()
    assert result.not_modified is True
    assert result.postings == []


async def test_conditional_headers_are_sent_when_known():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"jobs": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        ctx = FetchContext(
            client=client, config={"board_token": "x"}, adapter_name="greenhouse",
            etag='W/"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
        await get_adapter("greenhouse").fetch(ctx)
    finally:
        await client.aclose()

    assert seen["if-none-match"] == 'W/"abc"'
    assert seen["if-modified-since"].startswith("Wed, 21 Oct")


async def test_workday_retries_at_safe_limit_when_a_big_page_returns_nothing():
    """A capped tenant answers an oversized limit with zero rows, not a short page."""
    full = load_fixture("workday_nvidia")
    client = sequence_client([
        {"total": 40, "jobPostings": []},                       # limit=100 rejected
        {"total": 2, "jobPostings": full["jobPostings"][:2]},   # retry at limit=20
    ])
    try:
        ctx = FetchContext(
            client=client,
            config={"host": "https://h", "tenant": "t", "site": "s", "limit": 100},
            adapter_name="workday",
        )
        result = await get_adapter("workday").fetch(ctx)
    finally:
        await client.aclose()

    assert len(result.postings) == 2, "the capped-limit retry did not happen"


async def test_workday_paginates_a_capped_tenant_fully():
    full = load_fixture("workday_nvidia")["jobPostings"]
    total = len(full)
    page_size = 2
    pages = [
        {"total": total, "jobPostings": full[i : i + page_size]}
        for i in range(0, total, page_size)
    ]
    pages.append({"total": total, "jobPostings": []})

    client = sequence_client(pages)
    try:
        ctx = FetchContext(
            client=client,
            config={"host": "https://h", "tenant": "t", "site": "s", "limit": page_size},
            adapter_name="workday",
        )
        result = await get_adapter("workday").fetch(ctx)
    finally:
        await client.aclose()

    assert len(result.postings) == total


async def test_smartrecruiters_stops_at_total():
    page = load_fixture("smartrecruiters")
    page["totalFound"] = len(page["content"])
    client = sequence_client([page, {"totalFound": 0, "content": []}])
    try:
        ctx = FetchContext(client=client, config={"company": "Visa"}, adapter_name="smartrecruiters")
        result = await get_adapter("smartrecruiters").fetch(ctx)
    finally:
        await client.aclose()
    assert len(result.postings) == len(page["content"])
    assert client.call_count["n"] == 1, "paginated past totalFound"


# ── html and browser adapters ─────────────────────────────────────────────

SAMPLE_BOARD = """
<html><body>
  <div class="job"><a href="/jobs/1">Software Engineer Intern</a><span class="loc">Seattle, WA</span></div>
  <div class="job"><a href="/jobs/2">Data Science Intern</a><span class="loc">Remote</span></div>
</body></html>
"""


async def test_html_adapter_extracts_postings():
    client = text_client(SAMPLE_BOARD)
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers",
                "job_selector": "div.job",
                "location_selector": "span.loc",
            },
            adapter_name="html",
        )
        result = await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert [p.title for p in result.postings] == [
        "Software Engineer Intern",
        "Data Science Intern",
    ]
    assert result.postings[0].url == "https://example.com/jobs/1"
    assert result.postings[0].locations == ["Seattle, WA"]


async def test_html_selector_matching_nothing_raises_not_empty():
    """A broken selector and an empty board must not look the same (§13.10)."""
    client = text_client(SAMPLE_BOARD)
    try:
        ctx = FetchContext(
            client=client,
            config={"url": "https://example.com/careers", "job_selector": "div.no-such-class"},
            adapter_name="html",
        )
        with pytest.raises(AdapterError, match="matched nothing"):
            await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "base,path,expected",
    [
        # Root-relative resolves against the origin, not the base's directory.
        ("https://x.com/careers", "/jobs/1", "https://x.com/jobs/1"),
        ("https://x.com/careers/", "/jobs/1", "https://x.com/jobs/1"),
        ("https://x.com/careers", "jobs/1", "https://x.com/careers/jobs/1"),
        ("https://x.com", "/en/jobs/9", "https://x.com/en/jobs/9"),
        ("https://x.com/a", "https://y.com/b", "https://y.com/b"),
        ("https://x.com/a", "//cdn.y.com/b", "https://cdn.y.com/b"),
        ("https://x.com/a", None, "https://x.com/a"),
    ],
)
def test_absolute_url_resolution(base, path, expected):
    from jobwatch.adapters.base import absolute_url

    assert absolute_url(base, path) == expected


def test_dig_walks_dot_paths():
    payload = {"data": {"jobs": [{"title": "SWE Intern"}]}}
    assert dig(payload, "data.jobs.0.title") == "SWE Intern"
    assert dig(payload, "data.missing.0") is None
    assert dig(payload, None) is None


def test_browser_adapter_is_registered_even_without_playwright():
    """Phase 10 acceptance: the service starts with the extra absent."""
    from jobwatch.adapters.base import known_adapters

    assert "browser" in known_adapters()
    assert get_adapter("browser") is not None
