"""Pipeline tests: seeding, deduplication, and merge-key alert gating (§15).

These are the highest-value tests in the suite. Every anti-requirement in §13
that can be tested is tested here, because the two failures that destroy trust
in this system — a cold-start alert storm and duplicate alerts — are both
invisible until they happen to you at 3am.
"""

from __future__ import annotations

import itertools
import random

import pytest

from conftest import (
    FAKE_ALIASES,
    RecordingSender,
    add_company,
    add_source,
    get_source,
    posting,
    set_postings,
)
from jobwatch.normalize import merge_key

INTERN = "Software Engineer Intern"
OTHER = "Data Science Intern"


async def run(pipe, db, source_id):
    return await pipe.run_company([get_source(db, source_id)])


def queued(db) -> list[str]:
    import json

    return [
        json.loads(r["payload"]).get("title", "")
        for r in db.query("SELECT payload FROM outbox ORDER BY id")
    ]


# ── cold start ────────────────────────────────────────────────────────────


async def test_first_poll_seeds_silently(make_pipeline, seeded_db):
    """§13.1: the first poll of a source records everything and alerts nothing."""
    pipe, _sender = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=False)
    set_postings(seeded_db, sid, [posting(f"{INTERN} {i}", str(i)) for i in range(500)])

    report = await run(pipe, seeded_db, sid)

    assert report.alerted == 0
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 0
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 500
    assert seeded_db.scalar("SELECT seeded FROM sources WHERE id = ?", (sid,)) == 1
    assert seeded_db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE class_source = 'seed'", default=0
    ) == 500


async def test_alerts_begin_with_the_first_genuinely_new_posting(make_pipeline, seeded_db):
    pipe, sender = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=False)

    set_postings(seeded_db, sid, [posting(INTERN, "1")])
    await run(pipe, seeded_db, sid)
    assert queued(seeded_db) == []

    set_postings(seeded_db, sid, [posting(INTERN, "1"), posting(OTHER, "2")])
    report = await run(pipe, seeded_db, sid)

    assert report.alerted == 1
    assert queued(seeded_db) == [OTHER]
    await pipe.outbox.flush()
    assert len(sender.sent_titles) == 1


async def test_a_source_added_later_also_seeds_silently(make_pipeline, seeded_db):
    """Adding a second source to an existing company must not replay its board."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    first = add_source(seeded_db, "stripe", "fake", seeded=False)
    set_postings(seeded_db, first, [posting(INTERN, "1")])
    await run(pipe, seeded_db, first)

    second = add_source(seeded_db, "stripe", "fake1", priority=2, seeded=False)
    set_postings(seeded_db, second, [posting(OTHER, "9"), posting("Infra Intern", "10")])

    report = await run(pipe, seeded_db, second)
    assert report.alerted == 0
    assert queued(seeded_db) == []


async def test_restore_from_backup_missing_a_source_does_not_alert(make_pipeline, seeded_db):
    """A source whose rows are gone re-seeds rather than re-alerting (§7)."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=False)
    set_postings(seeded_db, sid, [posting(INTERN, str(i)) for i in range(20)])
    await run(pipe, seeded_db, sid)

    # Simulate a restore that lost this source's job rows and its seeded flag.
    seeded_db.execute("DELETE FROM jobs WHERE source_id = ?", (sid,))
    seeded_db.execute("DELETE FROM alerted_merges")
    seeded_db.execute("UPDATE sources SET seeded = 0 WHERE id = ?", (sid,))

    report = await run(pipe, seeded_db, sid)
    assert report.alerted == 0


# ── deduplication ─────────────────────────────────────────────────────────


async def test_polling_twice_produces_one_alert(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting(INTERN, "1")])

    for _ in range(5):
        await run(pipe, seeded_db, sid)

    assert queued(seeded_db) == [INTERN]
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 1


async def test_disappearing_and_reappearing_posting_does_not_realert(make_pipeline, seeded_db):
    """Pagination glitches make postings vanish and come back (§13.4)."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)

    set_postings(seeded_db, sid, [posting(INTERN, "1")])
    await run(pipe, seeded_db, sid)
    set_postings(seeded_db, sid, [])
    await run(pipe, seeded_db, sid)
    set_postings(seeded_db, sid, [posting(INTERN, "1")])
    await run(pipe, seeded_db, sid)

    assert queued(seeded_db) == [INTERN]
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 1, "row was deleted"


async def test_title_edits_do_not_realert_when_req_id_is_stable(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)

    set_postings(seeded_db, sid, [posting("Software Engineer Intern, Summer 2027", "R-1")])
    await run(pipe, seeded_db, sid)
    set_postings(seeded_db, sid, [posting("Software Engineering Internship (Austin, TX)", "R-1")])
    await run(pipe, seeded_db, sid)

    assert len(queued(seeded_db)) == 1


async def test_same_title_in_six_cities_is_one_alert(make_pipeline, seeded_db):
    """merge_key excludes location deliberately (§5)."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    cities = ["Austin, TX", "Seattle, WA", "NYC", "SF", "Chicago", "Boston"]
    set_postings(
        seeded_db,
        sid,
        [posting(f"{INTERN} ({city})", f"R-{i}", locations=[city]) for i, city in enumerate(cities)],
    )

    report = await run(pipe, seeded_db, sid)

    assert report.alerted == 1
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 6, "all rows persist"


async def test_flipping_to_fallback_does_not_duplicate_rows(make_pipeline, seeded_db):
    """dedup_key is scoped by the configured adapter, not the active one."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(
        seeded_db, "stripe", "fake", seeded=True,
        fallback_adapter="fake", fallback_config={"postings": [posting(INTERN, "1")]},
    )
    set_postings(seeded_db, sid, [posting(INTERN, "1")])
    seeded_db.execute(
        "UPDATE sources SET fallback_config = adapter_config WHERE id = ?", (sid,)
    )

    await run(pipe, seeded_db, sid)
    seeded_db.execute("UPDATE sources SET using_fallback = 1 WHERE id = ?", (sid,))
    await run(pipe, seeded_db, sid)

    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 1
    assert len(queued(seeded_db)) == 1


# ── cross-source: the default failure with multiple sources (§13.2) ───────


async def test_two_sources_same_job_produce_one_alert_linking_the_canonical_url(
    make_pipeline, seeded_db
):
    """Phase 4 acceptance, in full."""
    import json

    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    direct = add_source(seeded_db, "stripe", "fake", priority=1, seeded=True)
    board = add_source(seeded_db, "stripe", "fake1", priority=2, seeded=True)

    # Same role, different requisition IDs and different apply URLs.
    set_postings(seeded_db, direct, [posting(INTERN, "DIRECT-1", url="https://careers.stripe.com/1")])
    set_postings(seeded_db, board, [posting(INTERN, "GH-9", url="https://boards.greenhouse.io/9")])

    report = await pipe.run_company([get_source(seeded_db, direct), get_source(seeded_db, board)])

    assert report.alerted == 1, "the same job alerted twice"
    payloads = [json.loads(r["payload"]) for r in seeded_db.query("SELECT payload FROM outbox")]
    assert payloads[0]["url"] == "https://careers.stripe.com/1", "did not attribute to priority 1"
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 2, "both rows must persist"


async def test_disabling_the_direct_source_still_yields_the_board_result(
    make_pipeline, seeded_db
):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    direct = add_source(seeded_db, "stripe", "fake", priority=1, seeded=True)
    board = add_source(seeded_db, "stripe", "fake1", priority=2, seeded=True)
    seeded_db.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (direct,))
    set_postings(seeded_db, board, [posting(INTERN, "GH-9", url="https://boards.greenhouse.io/9")])

    report = await pipe.run_company([get_source(seeded_db, board)])

    assert report.alerted == 1
    assert queued(seeded_db) == [INTERN]


async def test_distinct_roles_from_two_sources_alert_once_each(make_pipeline, seeded_db):
    """Alerts equal the number of distinct merge keys (§15 cross-source test)."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    a = add_source(seeded_db, "stripe", "fake", priority=1, seeded=True)
    b = add_source(seeded_db, "stripe", "fake1", priority=2, seeded=True)

    set_postings(seeded_db, a, [posting(INTERN, "A1"), posting(OTHER, "A2")])
    set_postings(seeded_db, b, [posting(INTERN, "B1"), posting("Infra Intern", "B3")])

    await pipe.run_company([get_source(seeded_db, a), get_source(seeded_db, b)])

    distinct = {merge_key("stripe", t) for t in (INTERN, OTHER, "Infra Intern")}
    assert len(queued(seeded_db)) == len(distinct) == 3


@pytest.mark.parametrize("seed", range(12))
async def test_property_any_ordering_of_polls_alerts_exactly_once(
    make_pipeline, seeded_db, seed
):
    """§15: any sequence of poll results containing a requisition alerts once.

    Randomizes source count, ordering, repetition, and intervening
    disappearance. The invariant never moves: one alert per merge_key.
    """
    rng = random.Random(seed)
    pipe, _ = make_pipeline()
    add_company(seeded_db, "acme")

    source_ids = [
        add_source(seeded_db, "acme", FAKE_ALIASES[i], priority=i + 1, seeded=True)
        for i in range(rng.randint(1, 3))
    ]
    titles = [INTERN, OTHER, "Platform Engineering Intern"]
    for _round in range(rng.randint(3, 7)):
        for index, sid in enumerate(source_ids):
            present = [t for t in titles if rng.random() > 0.35]
            set_postings(
                seeded_db,
                sid,
                [posting(t, f"{index}-{titles.index(t)}") for t in present],
            )
        order = list(source_ids)
        rng.shuffle(order)
        await pipe.run_company([get_source(seeded_db, s) for s in order])

    sent = queued(seeded_db)
    assert len(sent) == len(set(merge_key("acme", t) for t in sent)), f"duplicate alerts: {sent}"
    assert set(sent) <= set(titles)


async def test_alerted_merges_survives_job_row_loss(make_pipeline, seeded_db):
    """The separate table is what makes suppression outlive the jobs table."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting(INTERN, "1")])
    await run(pipe, seeded_db, sid)

    seeded_db.execute("DELETE FROM jobs")
    await run(pipe, seeded_db, sid)

    assert len(queued(seeded_db)) == 1


# ── classification integration ────────────────────────────────────────────


async def test_rejected_postings_are_persisted_but_not_alerted(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(
        seeded_db, sid,
        [posting("Senior Staff Engineer", "1"), posting("PhD Research Intern", "2"), posting(INTERN, "3")],
    )

    report = await run(pipe, seeded_db, sid)

    assert report.alerted == 1
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 3
    assert queued(seeded_db) == [INTERN]


async def test_review_items_are_alerted_by_default(make_pipeline, seeded_db):
    """A false positive costs three seconds; a false negative costs an internship."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting("Sustainability Intern", "1")])

    report = await run(pipe, seeded_db, sid)

    assert report.alerted == 1
    assert seeded_db.scalar(
        "SELECT classification FROM jobs WHERE req_id = '1'"
    ) == "review"


async def test_queue_only_mode_suppresses_review_alerts(make_pipeline, seeded_db, settings):
    settings.classification.on_review = "queue_only"
    pipe, _ = make_pipeline()
    pipe.settings = settings
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting("Sustainability Intern", "1")])

    report = await run(pipe, seeded_db, sid)

    assert report.alerted == 0
    assert seeded_db.scalar("SELECT classification FROM jobs WHERE req_id='1'") == "review"


async def test_warm_tier_alerts_are_queued_as_digest(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "slowco", tier="warm")
    sid = add_source(seeded_db, "slowco", seeded=True)
    set_postings(seeded_db, sid, [posting(INTERN, "1")])

    await run(pipe, seeded_db, sid)

    assert seeded_db.scalar("SELECT kind FROM outbox LIMIT 1") == "digest"


# ── failure handling ──────────────────────────────────────────────────────


async def test_adapter_error_records_backoff_and_does_not_raise(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", "fake", {"raise": "board exploded"}, seeded=True)

    report = await run(pipe, seeded_db, sid)

    assert report.outcomes[0].outcome == "error"
    row = seeded_db.one("SELECT consecutive_failures, backoff_until FROM sources WHERE id = ?", (sid,))
    assert row["consecutive_failures"] == 1
    assert row["backoff_until"] is not None
    assert seeded_db.scalar("SELECT outcome FROM poll_log LIMIT 1") == "error"


async def test_five_failures_flip_the_source_to_its_fallback(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(
        seeded_db, "stripe", "fake", {"raise": "down"}, seeded=True,
        fallback_adapter="fake", fallback_config={"postings": [posting(INTERN, "1")]},
    )

    for _ in range(5):
        seeded_db.execute("UPDATE sources SET backoff_until = NULL WHERE id = ?", (sid,))
        await run(pipe, seeded_db, sid)

    assert seeded_db.scalar("SELECT using_fallback FROM sources WHERE id = ?", (sid,)) == 1

    # The fallback works, so the next poll succeeds and flips back to primary.
    seeded_db.execute("UPDATE sources SET backoff_until = NULL WHERE id = ?", (sid,))
    await run(pipe, seeded_db, sid)
    assert seeded_db.scalar("SELECT using_fallback FROM sources WHERE id = ?", (sid,)) == 0


async def test_304_is_recorded_without_reprocessing(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", "fake", {"not_modified": True}, seeded=True)

    report = await run(pipe, seeded_db, sid)

    assert report.outcomes[0].outcome == "not_modified"
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 0
    assert seeded_db.scalar("SELECT outcome FROM poll_log LIMIT 1") == "not_modified"


async def test_one_failing_source_does_not_stop_its_sibling(make_pipeline, seeded_db):
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    broken = add_source(seeded_db, "stripe", "fake", {"raise": "nope"}, priority=1, seeded=True)
    working = add_source(seeded_db, "stripe", "fake1", priority=2, seeded=True)
    set_postings(seeded_db, working, [posting(INTERN, "1")])

    report = await pipe.run_company(
        [get_source(seeded_db, broken), get_source(seeded_db, working)]
    )

    assert report.alerted == 1
    assert {o.outcome for o in report.outcomes} == {"error", "ok"}


async def test_dry_run_sends_nothing(make_pipeline, seeded_db):
    pipe, sender = make_pipeline(dry_run=True)
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting(INTERN, "1")])

    await run(pipe, seeded_db, sid)
    await pipe.outbox.flush()

    assert sender.messages == []
    assert seeded_db.scalar("SELECT status FROM outbox LIMIT 1") == "sent"


def test_merge_key_collision_sanity():
    """Distinct roles at one company must not share a merge key."""
    titles = [INTERN, OTHER, "Infra Intern", "Security Engineer Intern", "Hardware Intern"]
    keys = [merge_key("acme", t) for t in titles]
    assert len(set(keys)) == len(titles)
    assert all(a != b for a, b in itertools.combinations(keys, 2))


def test_recording_sender_is_not_accidentally_real():
    assert not hasattr(RecordingSender(), "webhook_url")
