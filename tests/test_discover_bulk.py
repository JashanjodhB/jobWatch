"""Bulk discovery: feed parsing, company aggregation, the ledger, and bounding.

The expensive parts of this module are the ones that decide *how many requests a
run makes*, so those get the most attention here: the URL preference that avoids
page fetches, the known-slug matching that keeps the queue small, and the ledger
that stops a weekly run re-probing the same failures forever.
"""

from __future__ import annotations

import json

import pytest

from jobwatch.discover_bulk import (
    FeedCompany,
    Ledger,
    ProbeOutcome,
    collect_companies,
    is_known,
    parse_feed,
    render_yaml,
    slugify,
)


def listing(**kw):
    """A feed row in the Simplify shape, overridable per test."""
    row = {
        "company_name": "Acme",
        "title": "Software Engineer Intern",
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/NYC/SWE-Intern_R1",
        "terms": ["Summer 2027"],
        "category": "Software",
        "active": True,
    }
    row.update(kw)
    return row


# ── parse_feed ────────────────────────────────────────────────────────────


def test_parse_feed_reads_the_simplify_shape():
    [item] = parse_feed([listing()])
    assert item.company_name == "Acme"
    assert item.terms == ("Summer 2027",)
    assert item.category == "Software"
    assert item.active is True


def test_parse_feed_accepts_the_season_string_shape():
    """The second default feed uses `season`, not `terms`. Both must work."""
    [item] = parse_feed([listing(terms=None, season="Summer 2027")])
    assert item.terms == ("Summer 2027",)


def test_parse_feed_skips_rows_without_a_name_or_url():
    rows = [listing(), listing(company_name=""), listing(url="")]
    assert len(parse_feed(rows)) == 1


def test_parse_feed_rejects_a_non_list_payload():
    with pytest.raises(ValueError, match="JSON array"):
        parse_feed({"listings": []})


# ── collect_companies ─────────────────────────────────────────────────────


def test_prefers_a_fingerprintable_url_over_a_plain_one():
    """A Workday URL costs no page fetch, so it must win regardless of order."""
    companies = collect_companies(
        parse_feed(
            [
                listing(url="https://acme.com/careers/openings"),
                listing(url="https://acme.wd1.myworkdayjobs.com/External/job/NYC/X_R1"),
            ]
        )
    )
    entry = companies["acme"]
    assert entry.fingerprintable is True
    assert "myworkdayjobs" in entry.careers_url


def test_aggregator_urls_are_never_used_as_careers_urls():
    companies = collect_companies(
        parse_feed([listing(url="https://simplify.jobs/p/abc-123")])
    )
    entry = companies["acme"]
    assert entry.careers_url == ""
    assert entry.listings == 1, "the company is still reported, only the URL is dropped"


def test_posting_tail_is_trimmed_from_the_careers_url():
    companies = collect_companies(
        parse_feed([listing(url="https://acme.wd1.myworkdayjobs.com/External/job/NYC/X_R1")])
    )
    assert companies["acme"].careers_url == "https://acme.wd1.myworkdayjobs.com/External"


def test_inactive_listings_are_excluded_by_default():
    assert collect_companies(parse_feed([listing(active=False)])) == {}
    assert collect_companies(parse_feed([listing(active=False)]), include_inactive=True)


def test_term_filter_selects_the_season():
    rows = parse_feed(
        [listing(company_name="A", terms=["Summer 2027"]),
         listing(company_name="B", terms=["Summer 2026"])]
    )
    got = collect_companies(rows, terms=["Summer 2027"])
    assert set(got) == {"a"}


def test_category_filter_selects_the_category():
    rows = parse_feed(
        [listing(company_name="A", category="Software"),
         listing(company_name="B", category="Quant")]
    )
    assert set(collect_companies(rows, categories=["quant"])) == {"b"}


def test_listings_and_terms_accumulate_per_company():
    rows = parse_feed(
        [listing(terms=["Summer 2027"]), listing(terms=["Fall 2026"]), listing()]
    )
    entry = collect_companies(rows)["acme"]
    assert entry.listings == 3
    assert entry.terms == {"Summer 2027", "Fall 2026"}


# ── slug matching ─────────────────────────────────────────────────────────


def test_slugify_matches_the_registry_convention():
    assert slugify("American Express") == "american-express"
    assert slugify("S&P Global") == "s-p-global"
    assert slugify("  Jump Trading  ") == "jump-trading"


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("stripe", True),                    # exact
        ("aqr-capital-management", True),    # feed is longer than the registry
        ("jump", True),                      # registry is longer than the feed
        ("stripe-labs-inc", True),           # extends a known slug
        ("striped", False),                  # not a segment boundary
        ("figma", False),                    # genuinely unknown
    ],
)
def test_is_known_tolerates_naming_length(slug, expected):
    known = {"stripe", "aqr-capital", "jump-trading"}
    assert is_known(slug, known) is expected


# ── ledger ────────────────────────────────────────────────────────────────


def test_ledger_round_trips(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path)
    ledger.record("acme", "verified", adapter="workday")
    ledger.save()

    reloaded = Ledger.load(path)
    assert reloaded.status("acme") == "verified"
    assert reloaded.entries["acme"]["adapter"] == "workday"
    assert reloaded.entries["acme"]["attempts"] == 1


def test_ledger_counts_attempts_across_runs(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path)
    ledger.record("acme", "failed", error="404")
    ledger.record("acme", "failed", error="404")
    assert ledger.entries["acme"]["attempts"] == 2


def test_a_corrupt_ledger_does_not_break_the_run(tmp_path):
    """One bad file costs a wasted run, never the command itself."""
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    assert Ledger.load(path).entries == {}


def test_missing_ledger_starts_empty(tmp_path):
    assert Ledger.load(tmp_path / "nope.json").entries == {}


def test_ledger_save_creates_its_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "ledger.json"
    ledger = Ledger.load(path)
    ledger.record("acme", "verified")
    ledger.save()
    assert json.loads(path.read_text(encoding="utf-8"))["companies"]["acme"]


# ── rendering ─────────────────────────────────────────────────────────────


def _outcome(slug="acme", *, verified=True, listings=3):
    from jobwatch.discover import Candidate

    candidate = Candidate(
        adapter="workday",
        config={"host": "https://acme.wd1.myworkdayjobs.com", "tenant": "acme"},
        confidence="high",
        evidence="from the URL",
        verified=verified,
        posting_count=12,
    )
    company = FeedCompany(
        slug=slug,
        display_name=slug.title(),
        careers_url="https://acme.wd1.myworkdayjobs.com/External",
        listings=listings,
        sample_title="SWE Intern",
        terms={"Summer 2027"},
    )
    return ProbeOutcome(company, candidate=candidate)


def test_render_yaml_emits_only_verified_blocks():
    out = render_yaml([_outcome("acme"), _outcome("ghost", verified=False)])
    assert "- slug: acme" in out
    assert "ghost" not in out


def test_rendered_yaml_parses_and_validates_as_a_registry_block():
    """The whole point is that the output can be pasted into companies.yaml."""
    import yaml

    from jobwatch.config import CompanySpec

    parsed = yaml.safe_load(render_yaml([_outcome()]))
    [spec] = [CompanySpec.model_validate(block) for block in parsed]
    assert spec.slug == "acme"
    assert spec.sources[0].adapter == "workday"


def test_render_yaml_applies_the_requested_tier():
    assert "tier: hot" in render_yaml([_outcome()], tier="hot")
    assert "tier: warm" in render_yaml([_outcome()])


def test_render_yaml_orders_by_listing_count():
    out = render_yaml([_outcome("small", listings=1), _outcome("big", listings=99)])
    assert out.index("- slug: big") < out.index("- slug: small")


def test_render_yaml_with_nothing_verified_is_still_valid_yaml():
    import yaml

    out = render_yaml([_outcome(verified=False)])
    assert yaml.safe_load(out) is None
