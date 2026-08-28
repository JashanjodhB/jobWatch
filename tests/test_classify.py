"""Classification: rules, the verdict cache, and their interaction (§8)."""

from __future__ import annotations

import pytest

from conftest import add_company, add_source, posting, set_postings
from jobwatch.classify.rules import RuleSet
from jobwatch.classify.verdicts import Classifier
from jobwatch.normalize import normalize_title


@pytest.fixture
def classifier(seeded_db) -> Classifier:
    return Classifier(seeded_db)


# ── rules ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineer Intern", "match"),
        ("Machine Learning Intern", "match"),
        ("Data Engineering Co-op", "match"),
        ("Summer Analyst - Quantitative Developer", "match"),
        ("2027 Summer Software Development Engineer Internship", "match"),
        ("Backend Engineer Intern", "match"),
        # The "Student" naming family: ByteDance, Google, John Deere and Zoox
        # all label real internships this way, so require_any covers it.
        ("Contract Student Worker - Data Scientist", "match"),
        # 'manager' is deliberately NOT excluded (2026-08-25): losing a real
        # posting costs more than triaging a product one. It reaches review
        # rather than match because, since the 2026-08-27 rewrite, bare
        # 'engineering' is no longer a role_any signal -- see below.
        ("Engineering Manager Intern", "review"),
        # ── the tech gate (rewritten 2026-08-27) ──────────────────────────
        # role_any used to carry a bare 'engineer(ing)?', so every
        # discipline matched. Computing disciplines still do:
        ("Cybersecurity Intern", "match"),
        ("DevOps Intern", "match"),
        ("IT Operations Intern", "match"),
        ("FPGA Intern", "match"),
        ("Electrical Engineering Intern", "match"),
        ("Web Developer Intern", "match"),
        # ...and non-computing ones are now rejected outright:
        ("Mining Engineer Intern", "reject"),
        ("Mechanical Engineering Intern", "reject"),
        ("Civil Engineering Intern", "reject"),
        ("Staff Accountant Intern", "reject"),
        ("Human Resources Intern", "reject"),
        # 'IT' is matched case-sensitively; an unanchored 'it' would make
        # every title carrying the pronoun a match.
        ("Make It Happen Intern", "review"),
        # The 'non-technical' lookbehind: without it this reads as a tech role.
        ("Student Intern - Non Technical", "review"),
        # Ambiguous by design -- no discipline named, so it lands in review
        # for triage rather than alerting.
        ("Engineering Intern", "review"),
        # Not over-rejected: 'supply chain' is deliberately absent from
        # exclude_any because it co-occurs with genuine data work.
        ("Data Analytics in Supply Chain Intern", "match"),
        # rejected by exclude_any
        ("PhD Research Intern, Robotics", "reject"),
        ("Returning Intern - Software Engineer", "reject"),
        ("MBA Intern, Strategy", "reject"),
        ("Sales Intern", "reject"),
        ("Software Engineer, New Grad", "reject"),
        # rejected for not being an internship at all -- this is what keeps a
        # plain manager role out now that exclude_any no longer names it.
        ("Senior Staff Software Engineer", "reject"),
        ("Director of Engineering", "reject"),
        ("Engineering Manager", "reject"),
        # university research assistantships stay out: '\bundergraduate\b' and
        # '\bresearch assistant\b' were considered and deliberately not added.
        ("Undergraduate Research Assistant", "reject"),
        # internship, but of an unrecognised kind
        ("Sustainability Intern", "review"),
        ("Legal Intern", "review"),
        ("Product Manager Intern - Ads Interface", "review"),
    ],
)
def test_rule_outcomes(classifier, title, expected):
    assert classifier.explain(title).classification == expected


def test_rules_run_against_the_original_title_not_the_normalized_one(classifier):
    """Normalization strips seasons and years, which these patterns depend on."""
    title = "2027 Summer Analyst"
    assert "summer" not in normalize_title(title)
    assert classifier.explain(title).classification in ("match", "review")


def test_location_exclude_rejects_only_when_every_location_is_excluded(classifier):
    title = "Software Engineer Intern"
    assert classifier.explain(title, ["Bangalore, India"]).classification == "reject"
    assert classifier.explain(title, ["Bangalore, India", "Seattle, WA"]).classification == "match"
    assert classifier.explain(title, []).classification == "match"


# ── location_require: the United States allow list ────────────────────────


@pytest.mark.parametrize(
    "location,expected",
    [
        # in range
        ("San Francisco, CA", "match"),
        ("US, CA, Santa Clara", "match"),
        ("San Mateo, CA, United States", "match"),
        ("New York, New York, USA", "match"),
        ("Remote - USA", "match"),
        ("United States - Remote", "match"),
        ("Mountain View, California", "match"),
        ("Washington, D.C.", "match"),
        ("San Francisco", "match"),
        ("NYC, SF", "match"),
        ("London, UK; Ontario, CAN; Remote-Friendly, United States", "match"),
        # out of range
        ("London, United Kingdom", "reject"),
        ("Singapore", "reject"),
        ("Tokyo, Japan", "reject"),
        ("Toronto, ON", "reject"),
        ("Sao Paulo, Brazil", "reject"),
        ("Shanghai, China", "reject"),
        # Amazon writes country-first, so an unanchored two-letter code would
        # read these as Delaware and Indiana.
        ("DE, BY, Munich", "reject"),
        ("IN, KA, Bengaluru", "reject"),
        # No geography at all. Unknown is not foreign — never drop these.
        ("2 Locations", "match"),
        ("Hybrid", "match"),
        ("Distributed", "match"),
    ],
)
def test_united_states_only(classifier, location, expected):
    assert classifier.explain("Software Engineer Intern", [location]).classification == expected


def test_one_location_in_range_is_enough(classifier):
    title = "Software Engineer Intern"
    assert classifier.explain(title, ["Tokyo, Japan", "Austin, TX"]).classification == "match"


def test_a_posting_with_no_location_is_never_location_rejected(classifier):
    """An adapter that failed to parse a location must not cost you a posting."""
    title = "Software Engineer Intern"
    assert classifier.explain(title, []).classification == "match"
    assert classifier.explain(title, None).classification == "match"
    assert classifier.explain(title, ["", "  "]).classification == "match"


def test_location_require_is_a_no_op_when_no_such_rule_exists():
    rules = RuleSet.from_patterns(
        {"require_any": [r"\bintern\b"], "role_any": [r"\bsoftware\b"]}
    )
    assert rules.evaluate("Software Intern", ["Tokyo, Japan"]).classification == "match"


def test_outcome_explains_itself(classifier):
    outcome = classifier.explain("Sustainability Intern").outcome
    assert outcome is not None
    assert outcome.require_hits and not outcome.role_hits
    assert "no role_any" in outcome.reason


def test_a_broken_pattern_is_reported_not_raised(seeded_db):
    seeded_db.execute(
        "INSERT INTO filter_rules(kind, pattern, enabled, created_at) VALUES(?,?,1,?)",
        ("role_any", "([unclosed", "2026-01-01T00:00:00Z"),
    )
    ruleset = RuleSet.load(seeded_db)
    assert len(ruleset.broken) == 1
    # Everything else still classifies.
    assert ruleset.evaluate("Software Engineer Intern").classification == "match"


def test_empty_ruleset_rejects_everything(db):
    """No require_any rules means nothing is an internship. Fail closed, not open."""
    assert RuleSet([]).evaluate("Software Engineer Intern").classification == "reject"


# ── verdict cache ─────────────────────────────────────────────────────────


def test_rules_cache_their_decisive_outcomes(classifier, seeded_db):
    classifier.classify("Software Engineer Intern")
    row = seeded_db.one(
        "SELECT verdict, source FROM title_verdicts WHERE normalized_title = ?",
        (normalize_title("Software Engineer Intern"),),
    )
    assert row["verdict"] == "match"
    assert row["source"] == "rules"


def test_review_outcomes_are_never_cached(classifier, seeded_db):
    classifier.classify("Sustainability Intern")
    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 0


def test_a_location_reject_is_never_cached_under_the_title(classifier, seeded_db):
    """The cache key is the normalized title, which carries no location.

    Cache a location-driven reject and the first London posting of a title
    suppresses the San Francisco one for good.
    """
    title = "Software Engineer Intern"
    assert classifier.classify(title, ["London, United Kingdom"]).classification == "reject"
    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 0
    assert classifier.classify(title, ["San Francisco, CA"]).classification == "match"


def test_location_screening_precedes_even_a_manual_verdict(classifier):
    """Where a job is is a property of the posting, not of its title."""
    classifier.record(normalize_title("Software Engineer Intern"), "match", source="manual")
    assert classifier.classify("Software Engineer Intern", ["Tokyo, Japan"]).classification == "reject"
    assert classifier.classify("Software Engineer Intern", ["Austin, TX"]).classification == "match"


def test_location_outcomes_are_not_decisive(classifier):
    outcome = classifier.explain("Software Engineer Intern", ["Tokyo, Japan"]).outcome
    assert outcome is not None
    assert outcome.location_dependent
    assert not outcome.is_decisive


def test_the_cache_is_consulted_before_the_rules(classifier):
    """A human verdict overrides what the rules would have said."""
    normalized = normalize_title("PhD Research Intern")
    assert classifier.explain("PhD Research Intern").classification == "reject"

    classifier.record(normalized, "match", source="manual", category="ai-ml")
    decision = classifier.classify("PhD Research Intern")

    assert decision.classification == "match"
    assert decision.class_source == "manual"
    assert decision.category == "ai-ml"


def test_a_verdict_generalizes_across_title_variants(classifier):
    classifier.record(normalize_title("Sustainability Intern"), "reject", source="manual")
    for variant in [
        "Sustainability Intern",
        "Sustainability Internship",
        "2027 Summer Sustainability Intern (Austin, TX)",
        "Sustainability Intern [REQ-9912]",
    ]:
        assert classifier.classify(variant).classification == "reject", variant


def test_a_manual_verdict_survives_a_rules_reclassification(classifier, seeded_db):
    normalized = normalize_title("Legal Intern")
    classifier.record(normalized, "match", source="manual")
    # A later rules pass must not overwrite it.
    classifier.record(normalized, "reject", source="rules")
    row = seeded_db.one(
        "SELECT verdict, source FROM title_verdicts WHERE normalized_title = ?", (normalized,)
    )
    assert row["source"] == "manual"
    assert row["verdict"] == "match"


def test_verdicts_survive_a_reopen(seeded_db, tmp_path):
    from jobwatch.db import Database

    Classifier(seeded_db).record(normalize_title("Legal Intern"), "match", source="manual")
    seeded_db.close()

    reopened = Database(seeded_db.path)
    reopened.migrate()
    try:
        assert Classifier(reopened).classify("Legal Internship").classification == "match"
    finally:
        reopened.close()


def test_forget_undoes_a_verdict(classifier, seeded_db):
    normalized = normalize_title("Legal Intern")
    classifier.record(normalized, "match", source="manual")
    classifier.forget(normalized)
    assert classifier.lookup(normalized) is None


def test_editing_rules_purges_rule_verdicts_but_keeps_human_ones(classifier, seeded_db):
    classifier.classify("Software Engineer Intern")           # cached by rules
    classifier.record(normalize_title("Legal Intern"), "match", source="manual")

    purged = classifier.purge_rule_verdicts()

    assert purged == 1
    remaining = seeded_db.query("SELECT normalized_title, source FROM title_verdicts")
    assert len(remaining) == 1
    assert remaining[0]["source"] == "manual"


def test_applying_a_verdict_restamps_existing_rows(classifier, seeded_db):
    seeded_db.execute(
        "INSERT OR IGNORE INTO companies(slug, display_name, tier, enabled) VALUES('x','X','hot',1)"
    )
    seeded_db.execute(
        "INSERT INTO sources(company_slug, adapter, adapter_config, priority) VALUES('x','fake','{}',1)"
    )
    normalized = normalize_title("Sustainability Intern")
    seeded_db.execute(
        "INSERT INTO jobs(dedup_key, merge_key, company_slug, source_id, title, "
        "normalized_title, locations, url, first_seen_at, last_seen_at, classification) "
        "VALUES('d','m','x',1,'Sustainability Intern',?,'[]','http://x','t','t','review')",
        (normalized,),
    )

    changed = classifier.apply_verdict_to_jobs(normalized, "reject", None)

    assert changed == 1
    assert seeded_db.scalar("SELECT classification FROM jobs WHERE dedup_key='d'") == "reject"


# ── end to end through the pipeline ───────────────────────────────────────


async def test_a_manual_verdict_classifies_the_next_posting_instantly(
    make_pipeline, seeded_db
):
    """Phase 6 acceptance."""
    from conftest import get_source

    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)

    pipe.classifier.record(
        normalize_title("Sustainability Intern"), "match", source="manual", category="infra"
    )
    set_postings(seeded_db, sid, [posting("2027 Sustainability Internship", "1")])

    await pipe.run_company([get_source(seeded_db, sid)])

    row = seeded_db.one("SELECT classification, class_source, category FROM jobs WHERE req_id='1'")
    assert row["classification"] == "match"
    assert row["class_source"] == "manual"
    assert row["category"] == "infra"
