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
    ("oracle", "oracle_amex",
     {"pod": "https://pod.example", "site_number": "CX_1"}, "items"),
    ("jibe", "jibe_amd", {"base": "https://careers.amd.com"}, "jobs"),
    ("phenom", "phenom_chewy", {"base": "https://careers.chewy.com"}, None),
    ("workable", "workable_ponyai", {"account": "pony-dot-ai"}, "jobs"),
    ("rippling", "rippling_spreeai", {"board": "spreeai"}, None),
    ("bamboohr", "bamboohr_specter", {"tenant": "specteraerospace"}, "result"),
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


# ── Eightfold, pcsx flavour (Microsoft) ───────────────────────────────────

MICROSOFT_CONFIG = {
    "base": "https://apply.careers.microsoft.com",
    "domain": "microsoft.com",
    "path": "/api/pcsx/search",
    "query": "intern",
}


def _pcsx(positions, count):
    """The pcsx envelope: results live under `data`, not at the top level."""
    return {"status": 200, "error": {"message": "", "body": ""},
            "data": {"positions": positions, "count": count}}


async def test_eightfold_parses_the_microsoft_pcsx_fixture():
    result = await fetch("eightfold", load_fixture("eightfold_microsoft"), MICROSOFT_CONFIG)
    assert result.postings
    for post in result.postings:
        assert post.title.strip()
        assert post.url.startswith("https://apply.careers.microsoft.com/careers/job/")


async def test_eightfold_maps_pcsx_camelcase_fields():
    """pcsx names every field differently; a miss here is a silently blank alert."""
    result = await fetch("eightfold", load_fixture("eightfold_microsoft"), MICROSOFT_CONFIG)
    post = result.postings[0]
    assert post.req_id == "200015233"          # displayJobId, not display_job_id
    assert post.title == "Research Sciences INTERN"
    assert post.department == "Research Sciences"
    assert post.posted_at and post.posted_at.endswith("Z")   # postedTs, not t_create


async def test_eightfold_renamed_pcsx_positions_raises():
    """§13.10 for the nested flavour: drift must raise, never return []."""
    mutated = copy.deepcopy(load_fixture("eightfold_microsoft"))
    mutated["data"]["positions_renamed"] = mutated["data"].pop("positions")

    with pytest.raises(AdapterError) as exc:
        await fetch("eightfold", mutated, MICROSOFT_CONFIG)
    assert "positions" in str(exc.value)


async def test_eightfold_uses_the_configured_path():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_pcsx([], 0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        ctx = FetchContext(client=client, config=MICROSOFT_CONFIG, adapter_name="eightfold")
        await get_adapter("eightfold").fetch(ctx)
    finally:
        await client.aclose()

    assert "/api/pcsx/search?domain=microsoft.com" in captured["url"]


async def test_eightfold_short_page_does_not_end_pagination_when_count_is_known():
    """The Microsoft regression: pcsx ignores `num` and always returns 10.

    Treating that short page as end-of-results would have collected the first
    ten postings and silently dropped the rest.
    """
    def page(offset, n=10):
        return _pcsx(
            [{"id": i, "name": f"Intern {i}", "positionUrl": f"/careers/job/{i}"}
             for i in range(offset, offset + n)],
            25,
        )

    client = sequence_client([page(0), page(10), page(20, 5)])
    try:
        ctx = FetchContext(
            client=client,
            config=MICROSOFT_CONFIG | {"num": 50},
            adapter_name="eightfold",
        )
        result = await get_adapter("eightfold").fetch(ctx)
    finally:
        await client.aclose()

    assert client.call_count["n"] == 3
    assert len(result.postings) == 25


async def test_eightfold_without_a_count_still_stops_on_a_short_page():
    """The classic flavour has no total, so a short page must remain terminal."""
    positions = [{"id": i, "name": f"Intern {i}", "canonicalPositionUrl": f"/job/{i}"}
                 for i in range(3)]
    client = sequence_client([{"positions": positions}])
    try:
        ctx = FetchContext(
            client=client,
            config={"base": "https://b", "domain": "d.com", "num": 50},
            adapter_name="eightfold",
        )
        result = await get_adapter("eightfold").fetch(ctx)
    finally:
        await client.aclose()

    assert client.call_count["n"] == 1
    assert len(result.postings) == 3


# ── jibe (iCIMS front end) ────────────────────────────────────────────────

JIBE_CONFIG = {"base": "https://careers.amd.com"}


def _jibe(records, total):
    return {"jobs": [{"data": r} for r in records], "count": total, "totalCount": total}


async def test_jibe_parses_the_amd_fixture():
    result = await fetch("jibe", load_fixture("jibe_amd"), JIBE_CONFIG)
    assert result.postings
    for post in result.postings:
        assert post.title.strip()
        assert post.url.startswith("https://")
        assert post.req_id


async def test_jibe_links_at_the_ats_not_the_careers_site():
    """apply_url points into iCIMS; that is where a human actually applies."""
    result = await fetch("jibe", load_fixture("jibe_amd"), JIBE_CONFIG)
    assert any("icims.com" in p.url for p in result.postings)


async def test_jibe_missing_data_envelope_raises():
    """§13.10: a bare record means the envelope moved, and must be loud."""
    mutated = copy.deepcopy(load_fixture("jibe_amd"))
    mutated["jobs"][0] = mutated["jobs"][0]["data"]
    with pytest.raises(AdapterError, match="data"):
        await fetch("jibe", mutated, JIBE_CONFIG)


async def test_jibe_job_without_apply_url_falls_back_to_the_slug():
    payload = _jibe([{"title": "Intern", "req_id": "1", "slug": "1"}], 1)
    result = await fetch("jibe", payload, JIBE_CONFIG)
    assert result.postings[0].url == "https://careers.amd.com/careers-home/jobs/1"


async def test_jibe_job_with_neither_apply_url_nor_slug_raises():
    payload = _jibe([{"title": "Intern"}], 1)
    with pytest.raises(AdapterError, match="apply_url"):
        await fetch("jibe", payload, JIBE_CONFIG)


async def test_jibe_strips_the_leading_space_from_categories():
    """`category` arrives as [" Student / Intern / Temp"] -- note the space."""
    payload = _jibe(
        [{"title": "Intern", "req_id": "1", "apply_url": "https://x.icims.com/jobs/1",
          "category": [" Student / Intern / Temp"]}], 1
    )
    result = await fetch("jibe", payload, JIBE_CONFIG)
    assert result.postings[0].department == "Student / Intern / Temp"


async def test_jibe_prefers_the_multi_site_locations_list():
    payload = _jibe(
        [{"title": "Intern", "req_id": "1", "apply_url": "https://x.icims.com/jobs/1",
          "full_location": "Austin, Texas",
          "locations": ["Austin, Texas", "Santa Clara, California"]}], 1
    )
    result = await fetch("jibe", payload, JIBE_CONFIG)
    assert result.postings[0].locations == ["Austin, Texas", "Santa Clara, California"]


async def test_jibe_caps_the_page_size_at_a_hundred():
    """`limit=200` returns an empty list rather than an error -- silently fatal."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=_jibe([], 0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        ctx = FetchContext(
            client=client, config=JIBE_CONFIG | {"limit": 500}, adapter_name="jibe"
        )
        await get_adapter("jibe").fetch(ctx)
    finally:
        await client.aclose()

    assert captured["limit"] == "100"


async def test_jibe_paginates_until_the_total_is_reached():
    def page(start, n):
        return _jibe(
            [{"title": f"Intern {i}", "req_id": str(i),
              "apply_url": f"https://x.icims.com/jobs/{i}"}
             for i in range(start, start + n)],
            250,
        )

    client = sequence_client([page(0, 100), page(100, 100), page(200, 50)])
    try:
        ctx = FetchContext(
            client=client, config=JIBE_CONFIG, adapter_name="jibe"
        )
        result = await get_adapter("jibe").fetch(ctx)
    finally:
        await client.aclose()

    assert client.call_count["n"] == 3
    assert len(result.postings) == 250


async def test_jibe_pages_are_one_based():
    """page=0 returns the same rows as page=1, which would double-count."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("page"))
        return httpx.Response(200, json=_jibe([], 0))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        ctx = FetchContext(client=client, config=JIBE_CONFIG, adapter_name="jibe")
        await get_adapter("jibe").fetch(ctx)
    finally:
        await client.aclose()

    assert seen[0] == "1"


ORACLE_CONFIG = {"pod": "https://pod.example", "site_number": "CX_1"}


async def test_oracle_maps_the_amex_fixture():
    result = await fetch("oracle", load_fixture("oracle_amex"), ORACLE_CONFIG)
    assert len(result.postings) == 4
    post = result.postings[0]
    assert post.req_id == "26012042"
    assert post.title
    assert post.url.startswith("https://")


async def test_oracle_prefers_the_public_site_url_for_apply_links():
    """Alerts must link where a human applies, not at the opaque Oracle pod."""
    config = ORACLE_CONFIG | {"site_url": "https://careers.americanexpress.com/en/sites/CX_1"}
    result = await fetch("oracle", load_fixture("oracle_amex"), config)
    assert result.postings[0].url == (
        "https://careers.americanexpress.com/en/sites/CX_1/job/26012042"
    )


async def test_oracle_falls_back_to_the_pod_url_without_site_url():
    result = await fetch("oracle", load_fixture("oracle_amex"), ORACLE_CONFIG)
    assert result.postings[0].url == (
        "https://pod.example/hcmUI/CandidateExperience/en/sites/CX_1/job/26012042"
    )


async def test_oracle_turns_a_bare_date_into_an_instant():
    result = await fetch("oracle", load_fixture("oracle_amex"), ORACLE_CONFIG)
    assert result.postings[0].posted_at.endswith("T00:00:00Z")


async def test_oracle_keeps_state_codes_uppercase():
    """Title-casing SHOUTED locations must not turn 'FL' into 'Fl'."""
    from jobwatch.adapters.oracle import _tidy

    assert _tidy("SUNRISE, FL, United States") == "Sunrise, FL, USA"
    assert _tidy("BRIGHTON, EAST SUSSEX, United Kingdom") == "Brighton, East Sussex, UK"


async def test_oracle_reads_an_empty_board_as_empty_not_broken():
    """A search matching nothing legitimately omits requisitionList."""
    mutated = copy.deepcopy(load_fixture("oracle_amex"))
    del mutated["items"][0]["requisitionList"]
    mutated["items"][0]["TotalJobsCount"] = 0

    result = await fetch("oracle", mutated, ORACLE_CONFIG)
    assert result.postings == []


async def test_oracle_missing_requisition_list_with_a_nonzero_total_raises():
    """The `expand=` param being dropped must be loud, not a silent empty board."""
    mutated = copy.deepcopy(load_fixture("oracle_amex"))
    del mutated["items"][0]["requisitionList"]
    assert mutated["items"][0]["TotalJobsCount"] > 0

    with pytest.raises(AdapterError, match="requisitionList"):
        await fetch("oracle", mutated, ORACLE_CONFIG)


async def test_oracle_renamed_requisition_list_raises():
    mutated = copy.deepcopy(load_fixture("oracle_amex"))
    block = mutated["items"][0]
    block["requisitionList_renamed"] = block.pop("requisitionList")

    with pytest.raises(AdapterError, match="requisitionList"):
        await fetch("oracle", mutated, ORACLE_CONFIG)


async def test_oracle_empty_items_envelope_raises():
    mutated = copy.deepcopy(load_fixture("oracle_amex"))
    mutated["items"] = []
    with pytest.raises(AdapterError, match="items"):
        await fetch("oracle", mutated, ORACLE_CONFIG)


async def test_oracle_missing_req_id_raises():
    mutated = copy.deepcopy(load_fixture("oracle_amex"))
    del mutated["items"][0]["requisitionList"][0]["Id"]
    with pytest.raises(AdapterError, match="Id"):
        await fetch("oracle", mutated, ORACLE_CONFIG)


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


async def test_workday_one_missing_external_path_drops_only_that_row():
    """Narrowed 2026-08-22 from "any missing externalPath raises".

    One unroutable row among good ones is bad data and costs one posting;
    *every* row unroutable is a renamed field and still raises. The all-or-
    nothing version lost Accenture's entire 211-posting board to a single
    malformed record. See the two tests at the end of this file.
    """
    mutated = copy.deepcopy(load_fixture("workday_nvidia"))
    kept = len(mutated["jobPostings"]) - 1
    del mutated["jobPostings"][0]["externalPath"]
    result = await fetch(
        "workday", mutated, {"host": "https://h", "tenant": "t", "site": "s"}
    )
    assert len(result.postings) == kept


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


async def test_workday_stops_at_max_pages_without_erroring():
    """A bounded probe returns a partial board, not an AdapterError.

    Bulk discovery only needs to know the tenant answers. Scanning all of it is
    ~46 requests per company, which is the traffic that gets a residential IP
    blocked.
    """
    full = load_fixture("workday_nvidia")["jobPostings"]
    page_size = 2
    pages = [
        {"total": 10_000, "jobPostings": full[i : i + page_size]}
        for i in range(0, len(full), page_size)
    ]

    client = sequence_client(pages)
    try:
        ctx = FetchContext(
            client=client,
            config={"host": "https://h", "tenant": "t", "site": "s", "limit": page_size},
            adapter_name="workday",
            max_pages=1,
        )
        result = await get_adapter("workday").fetch(ctx)
    finally:
        await client.aclose()

    assert client.call_count["n"] == 1, "bounded probe kept paginating"
    assert len(result.postings) == page_size


async def test_smartrecruiters_stops_at_max_pages():
    page = load_fixture("smartrecruiters")
    page["totalFound"] = 10_000  # would otherwise keep paging
    client = sequence_client([page])
    try:
        ctx = FetchContext(
            client=client,
            config={"company": "Visa"},
            adapter_name="smartrecruiters",
            max_pages=1,
        )
        result = await get_adapter("smartrecruiters").fetch(ctx)
    finally:
        await client.aclose()

    assert client.call_count["n"] == 1
    assert len(result.postings) == len(page["content"])


async def test_eightfold_stops_at_max_pages():
    fixture = load_fixture("eightfold_netflix")
    positions = fixture["positions"]
    client = sequence_client([{"count": 10_000, "positions": positions}])
    try:
        ctx = FetchContext(
            client=client,
            config={"base": "https://b", "domain": "d.com", "num": len(positions)},
            adapter_name="eightfold",
            max_pages=1,
        )
        result = await get_adapter("eightfold").fetch(ctx)
    finally:
        await client.aclose()

    assert client.call_count["n"] == 1
    assert len(result.postings) == len(positions)


async def test_max_pages_unset_still_paginates_fully():
    """The bound is opt-in: a normal poll must still collect the whole board."""
    full = load_fixture("workday_nvidia")["jobPostings"]
    page_size = 2
    pages = [
        {"total": len(full), "jobPostings": full[i : i + page_size]}
        for i in range(0, len(full), page_size)
    ]
    pages.append({"total": len(full), "jobPostings": []})

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

    assert len(result.postings) == len(full)
    assert client.call_count["n"] > 1


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


# ── html paging ───────────────────────────────────────────────────────────


def _paging_client(pages: dict[int, str], param: str, default: int = 0):
    """Serves a different board per value of `param`, and records the values."""
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.params.get(param)
        value = int(raw) if raw is not None else default
        asked.append(value)
        return httpx.Response(200, text=pages.get(value, "<html><body></body></html>"),
                              headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.asked = asked  # type: ignore[attr-defined]
    return client


def _board(ids):
    rows = "".join(f'<div class="job"><a href="/jobs/{i}">Intern {i}</a></div>' for i in ids)
    return f"<html><body>{rows}</body></html>"


async def test_html_adapter_without_page_param_makes_one_request():
    """Paging is opt-in: an unpaged board must not gain extra traffic (§13.5)."""
    client = _paging_client({0: SAMPLE_BOARD}, "jobOffset")
    try:
        ctx = FetchContext(
            client=client,
            config={"url": "https://example.com/careers", "job_selector": "div.job"},
            adapter_name="html",
        )
        result = await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert len(client.asked) == 1
    assert len(result.postings) == 2


async def test_html_adapter_pages_by_offset():
    """Avature's convention: an offset that grows by the page size."""
    pages = {0: _board([1, 2, 3]), 10: _board([4, 5, 6]), 20: _board([7])}
    client = _paging_client(pages, "jobOffset")
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers",
                "job_selector": "div.job",
                "page_param": "jobOffset",
                "page_step": 10,
            },
            adapter_name="html",
        )
        result = await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert client.asked == [0, 10, 20, 30]   # 30 is empty and ends it
    assert len(result.postings) == 7


async def test_html_adapter_pages_by_page_number():
    """Google's convention: a 1-based page number that grows by one."""
    pages = {1: _board([1, 2]), 2: _board([3, 4])}
    client = _paging_client(pages, "page", default=1)
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers",
                "job_selector": "div.job",
                "page_param": "page",
                "page_start": 1,
                "page_step": 1,
            },
            adapter_name="html",
        )
        result = await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert client.asked == [1, 2, 3]
    assert len(result.postings) == 4


async def test_html_adapter_stops_when_a_page_repeats_the_previous_one():
    """Boards that clamp the offset re-serve the last page forever."""
    same = _board([1, 2])
    client = _paging_client({0: same, 10: same, 20: same}, "jobOffset")
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers",
                "job_selector": "div.job",
                "page_param": "jobOffset",
                "page_step": 10,
                "max_pages": 12,
            },
            adapter_name="html",
        )
        result = await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert client.asked == [0, 10]
    assert len(result.postings) == 2


async def test_html_adapter_replaces_an_existing_page_param():
    """The configured URL may already carry the parameter."""
    client = _paging_client({0: _board([1]), 10: _board([2])}, "jobOffset")
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers?jobOffset=99&x=1",
                "job_selector": "div.job",
                "page_param": "jobOffset",
                "page_step": 10,
            },
            adapter_name="html",
        )
        await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert 99 not in client.asked
    assert client.asked[:2] == [0, 10]


async def test_html_adapter_empty_first_page_still_raises_when_paging():
    """§13.10 survives paging: drift on page one is loud, not an empty board."""
    client = _paging_client({}, "jobOffset")
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers",
                "job_selector": "div.job",
                "page_param": "jobOffset",
            },
            adapter_name="html",
        )
        with pytest.raises(AdapterError, match="matched nothing"):
            await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()


async def test_html_adapter_respects_max_pages_bound():
    pages = {i * 10: _board([i * 10 + 1]) for i in range(20)}
    client = _paging_client(pages, "jobOffset")
    try:
        ctx = FetchContext(
            client=client,
            config={
                "url": "https://example.com/careers",
                "job_selector": "div.job",
                "page_param": "jobOffset",
                "page_step": 10,
            },
            adapter_name="html",
            max_pages=3,
        )
        result = await get_adapter("html").fetch(ctx)
    finally:
        await client.aclose()

    assert client.asked == [0, 10, 20]
    assert len(result.postings) == 3


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


# ── workable / rippling / bamboohr ────────────────────────────────────────


async def test_workable_links_to_the_posting_not_the_bare_form():
    """`application_url` is the form; `url` is the description page.

    Matched on the suffix, not `"/apply" in url` — every Workable URL is on
    apply.workable.com, so the substring is always present.
    """
    result = await fetch(
        "workable", load_fixture("workable_ponyai"), {"account": "pony-dot-ai"}
    )
    assert result.postings
    assert all(not p.url.rstrip("/").endswith("/apply") for p in result.postings)


async def test_workable_prefers_the_locations_array_over_the_flat_fields():
    payload = {"jobs": [{
        "title": "SWE Intern", "shortcode": "AB12", "url": "https://apply.workable.com/j/AB12",
        "city": "Fremont", "state": "California", "country": "United States",
        "locations": [
            {"city": "Austin", "region": "Texas", "country": "United States"},
            {"city": "Boston", "region": "Massachusetts", "country": "United States"},
        ],
    }]}
    result = await fetch("workable", payload, {"account": "x"})
    assert result.postings[0].locations == [
        "Austin, Texas, United States", "Boston, Massachusetts, United States"
    ]


async def test_workable_falls_back_to_the_flat_location_fields():
    payload = {"jobs": [{
        "title": "SWE Intern", "shortcode": "AB12", "url": "https://apply.workable.com/j/AB12",
        "city": "Fremont", "state": "California", "country": "United States",
    }]}
    result = await fetch("workable", payload, {"account": "x"})
    assert result.postings[0].locations == ["Fremont, California, United States"]


async def test_rippling_reads_a_bare_top_level_array():
    """The response is a JSON array, not an object with a `jobs` key."""
    result = await fetch("rippling", load_fixture("rippling_spreeai"), {"board": "spreeai"})
    assert result.postings
    assert all(p.title for p in result.postings)


async def test_rippling_unwraps_label_objects():
    """`department` and `workLocation` are {id,label}, not strings."""
    payload = [{
        "uuid": "u1", "name": "ML Intern", "url": "https://ats.rippling.com/x/jobs/u1",
        "department": {"id": "Engineering", "label": "Engineering"},
        "workLocation": {"id": "SF", "label": "Hybrid (San Francisco, California, US)"},
    }]
    result = await fetch("rippling", payload, {"board": "x"})
    assert result.postings[0].locations == ["Hybrid (San Francisco, California, US)"]


async def test_bamboohr_builds_the_apply_url_from_the_tenant():
    """The payload carries no URL at all; it has to be assembled."""
    result = await fetch(
        "bamboohr", load_fixture("bamboohr_specter"), {"tenant": "specteraerospace"}
    )
    assert result.postings
    for post in result.postings:
        assert post.url.startswith("https://specteraerospace.bamboohr.com/careers/")
        assert post.url.rstrip("/").split("/")[-1] == post.req_id


async def test_bamboohr_raises_when_a_posting_has_no_id():
    """Without an id the apply URL would be wrong rather than missing (§13.10)."""
    payload = {"result": [{"jobOpeningName": "SWE Intern", "location": {"city": "Austin"}}]}
    with pytest.raises(AdapterError, match="no id"):
        await fetch("bamboohr", payload, {"tenant": "x"})


async def test_bamboohr_reads_the_real_location_not_the_null_ats_one():
    payload = {"result": [{
        "id": "9", "jobOpeningName": "SWE Intern",
        "location": {"city": "Peabody", "state": "Massachusetts"},
        "atsLocation": {"country": None, "state": None, "province": None, "city": None},
    }]}
    result = await fetch("bamboohr", payload, {"tenant": "x"})
    assert result.postings[0].locations == ["Peabody, Massachusetts"]


# ── browser: reading a sub-frame (iCIMS) ──────────────────────────────────


class _FakeFrame:
    def __init__(self, url: str, html: str = "") -> None:
        self.url = url
        self._html = html

    async def content(self) -> str:
        return self._html


class _FakePage:
    """Just enough of a Playwright page for `_frame_content`."""

    def __init__(self, frames: list[_FakeFrame]) -> None:
        self.main_frame = frames[0]
        self.frames = frames


async def test_browser_reads_the_named_sub_frame():
    """iCIMS renders the wrapper in the main frame and the jobs in a child."""
    from jobwatch.adapters.browser import _frame_content

    page = _FakePage([
        _FakeFrame("https://careers-x.icims.com/jobs/search?ss=1", "<html>wrapper</html>"),
        _FakeFrame("https://careers-x.icims.com/jobs/search?ss=1&in_iframe=1", "<li>job</li>"),
    ])
    assert await _frame_content(page, "in_iframe=1", "browser") == "<li>job</li>"


async def test_browser_never_falls_back_to_the_wrapper_frame():
    """Returning the main frame would surface as 'job_selector matched nothing',
    which blames the selector for a missing frame (§13.10)."""
    from jobwatch.adapters.browser import _frame_content

    page = _FakePage([
        _FakeFrame("https://careers-x.icims.com/jobs/search", "<html>wrapper</html>"),
        _FakeFrame("https://www.googletagmanager.com/ns.html", "<html>gtm</html>"),
    ])
    with pytest.raises(AdapterError) as excinfo:
        await _frame_content(page, "in_iframe=1", "browser")
    assert "in_iframe=1" in str(excinfo.value)
    assert "googletagmanager" in str(excinfo.value)


async def test_browser_frame_error_says_so_when_there_are_no_sub_frames():
    from jobwatch.adapters.browser import _frame_content

    page = _FakePage([_FakeFrame("https://example.com/careers", "<html></html>")])
    with pytest.raises(AdapterError, match="no sub-frames"):
        await _frame_content(page, "in_iframe=1", "browser")


# ── phenom (POST /widgets) ────────────────────────────────────────────────

PHENOM_CONFIG = {"base": "https://careers.chewy.com"}


def _phenom(jobs):
    return {"refineSearch": {"data": {"jobs": jobs}}}


async def test_phenom_parses_the_chewy_fixture():
    result = await fetch("phenom", load_fixture("phenom_chewy"), PHENOM_CONFIG)
    assert len(result.postings) == 5
    assert result.postings[0].title == "Category Analyst"


async def test_phenom_links_at_the_ats_not_the_careers_site():
    """applyUrl points at the real ATS behind Phenom -- keep it, do not rewrite."""
    result = await fetch("phenom", load_fixture("phenom_chewy"), PHENOM_CONFIG)
    assert all(p.url.startswith("http") for p in result.postings)
    assert any("myworkdaysite.com" in p.url for p in result.postings)


async def test_phenom_missing_refine_search_envelope_raises():
    """The envelope moving must be loud, not an empty board (§13.10)."""
    with pytest.raises(AdapterError, match="envelope changed"):
        await fetch("phenom", {"somethingElse": {}}, PHENOM_CONFIG)


async def test_phenom_session_probe_response_raises():
    """`eagerLoadRefineSearchSession` answers this shape and carries no jobs.

    Treating it as an empty board is exactly the silent failure that made this
    platform look unreachable in the first place.
    """
    with pytest.raises(AdapterError, match="session probe"):
        await fetch("phenom", {"refineSearch": {"tokenAvailable": True}}, PHENOM_CONFIG)


async def test_phenom_absent_jobs_key_is_an_empty_board_not_an_error():
    result = await fetch("phenom", {"refineSearch": {"data": {}}}, PHENOM_CONFIG)
    assert result.postings == []


async def test_phenom_job_without_apply_url_raises():
    with pytest.raises(AdapterError, match="no applyUrl"):
        await fetch("phenom", _phenom([{"title": "Intern", "jobId": "1"}]), PHENOM_CONFIG)


async def test_phenom_prefers_multi_location_over_the_scalars():
    payload = _phenom([{
        "title": "Intern", "jobId": "1", "applyUrl": "https://x.test/1",
        "multi_location": ["Boston, MA", "Austin, TX"],
        "cityState": "Boston, Massachusetts",
    }])
    result = await fetch("phenom", payload, PHENOM_CONFIG)
    assert result.postings[0].locations == ["Boston, MA", "Austin, TX"]


async def test_phenom_falls_back_to_city_state_country_parts():
    payload = _phenom([{
        "title": "Intern", "jobId": "1", "applyUrl": "https://x.test/1",
        "city": "Boston", "state": "Massachusetts", "country": "United States",
    }])
    result = await fetch("phenom", payload, PHENOM_CONFIG)
    assert result.postings[0].locations == ["Boston, Massachusetts, United States"]


# ── workday: one bad row must not lose the board ──────────────────────────

WD_CONFIG = {"host": "https://x.wd5.myworkdayjobs.com", "tenant": "x", "site": "S"}


def _wd(postings, total=None):
    body = {"jobPostings": postings}
    if total is not None:
        body["total"] = total
    return body


async def test_workday_skips_a_single_row_without_external_path():
    """Accenture serves one such row among hundreds of good ones.

    Aborting the scan for it loses the whole board, which is a worse failure
    than dropping the row.
    """
    payload = _wd([
        {"title": "Intern A", "externalPath": "/job/A_JR1", "bulletFields": ["JR1"]},
        {"bulletFields": ["R00342699", "Location Negotiable"]},
        {"title": "Intern B", "externalPath": "/job/B_JR2", "bulletFields": ["JR2"]},
    ], total=3)
    result = await fetch("workday", payload, WD_CONFIG)
    assert [p.title for p in result.postings] == ["Intern A", "Intern B"]


async def test_workday_raises_when_every_row_lacks_external_path():
    """A renamed field looks like this, and must stay loud (§13.10)."""
    payload = _wd([{"bulletFields": ["JR1"]}, {"bulletFields": ["JR2"]}], total=2)
    with pytest.raises(AdapterError, match="the field was renamed"):
        await fetch("workday", payload, WD_CONFIG)


async def test_workday_non_object_posting_still_raises():
    """Only the missing-externalPath case is recoverable; shape drift is not."""
    with pytest.raises(AdapterError, match="expected jobPostings entries"):
        await fetch("workday", _wd(["not-an-object"], total=1), WD_CONFIG)
