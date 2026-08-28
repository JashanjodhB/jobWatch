"""Backfill tests: recovering the postings a silent cold start swallowed.

The two failures that matter here are opposites. Backfilling too little leaves
the backlog invisible, which is the bug this command exists to fix. Backfilling
too much re-notifies jobs you have already seen — and because `alerted_merges`
cannot tell a seeding suppression from a real alert, getting that guard wrong is
silent until a re-run floods the webhook.
"""

from __future__ import annotations

import json

import pytest

from conftest import add_company, add_source, distinct_title, get_source, posting, set_postings
from jobwatch.backfill import BackfillScope, backfill
from jobwatch.notify.outbox import Outbox

INTERN = "Software Engineer Intern"
REJECT = "Senior Staff Accountant"


@pytest.fixture
def silent_pipeline(make_pipeline, settings):
    """A pipeline that still seeds the old way, so there is a backlog to fix."""
    settings.classification.on_seed = "silent"
    pipe, sender = make_pipeline()
    pipe.settings = settings
    return pipe, sender


async def seed(pipe, db, slug, postings, *, tier="hot"):
    add_company(db, slug, tier=tier)
    sid = add_source(db, slug, seeded=False)
    set_postings(db, sid, postings)
    await pipe.run_company([get_source(db, sid)])
    return sid


def queued_titles(db) -> list[str]:
    return [
        json.loads(r["payload"])["title"]
        for r in db.query("SELECT payload FROM outbox ORDER BY id")
    ]


def run_backfill(db, settings, scope=None, *, dry_run=False):
    return backfill(db, settings, Outbox(db), scope, dry_run=dry_run)


async def test_backfill_surfaces_what_seeding_buried(silent_pipeline, seeded_db, settings):
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1"), posting(REJECT, "2")])
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 0

    report = run_backfill(seeded_db, settings)

    assert report.scanned == 2
    assert report.reclassified == 2
    assert report.queued == 1
    assert queued_titles(seeded_db) == [INTERN]


async def test_backfill_reclassifies_rejects_too(silent_pipeline, seeded_db, settings):
    """The rejects are the point of reclassifying in place: a seeded row is
    `reject` because nothing ever looked at it, not because a rule said so."""
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(REJECT, "2")])

    run_backfill(seeded_db, settings)

    row = seeded_db.one("SELECT classification, class_source FROM jobs WHERE req_id = '2'")
    assert row["classification"] == "reject"
    assert row["class_source"] == "backfill", "no longer an untriaged seed row"


async def test_running_backfill_twice_queues_nothing_the_second_time(
    silent_pipeline, seeded_db, settings
):
    """The guard that makes this command safe to re-run (see module docstring)."""
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")])

    first = run_backfill(seeded_db, settings)
    second = run_backfill(seeded_db, settings)

    assert first.queued == 1
    assert second.scanned == 0, "the rows are no longer class_source='seed'"
    assert second.queued == 0
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 1


async def test_backfill_never_touches_a_merge_that_really_alerted(
    silent_pipeline, seeded_db, settings
):
    """A second source seeded later carries jobs the first source already
    alerted on. Those must stay put even though their rows say 'seed'."""
    pipe, _ = silent_pipeline
    add_company(seeded_db, "stripe")

    # First source is live and alerts normally.
    first = add_source(seeded_db, "stripe", "fake", seeded=True)
    set_postings(seeded_db, first, [posting(INTERN, "1")])
    await pipe.run_company([get_source(seeded_db, first)])
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 1

    # Second source seeds silently, carrying the same job under a new req_id.
    second = add_source(seeded_db, "stripe", "fake1", priority=2, seeded=False)
    set_postings(seeded_db, second, [posting(INTERN, "99")])
    await pipe.run_company([get_source(seeded_db, second)])

    report = run_backfill(seeded_db, settings)

    assert report.queued == 0
    assert report.skipped_already_alerted == 1
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 1


async def test_one_merge_key_queues_once_across_sources(
    silent_pipeline, seeded_db, settings
):
    pipe, _ = silent_pipeline
    add_company(seeded_db, "stripe")
    for i, adapter in enumerate(("fake", "fake1")):
        sid = add_source(seeded_db, "stripe", adapter, priority=i + 1, seeded=False)
        set_postings(seeded_db, sid, [posting(INTERN, f"{i}0")])
        await pipe.run_company([get_source(seeded_db, sid)])

    report = run_backfill(seeded_db, settings)

    assert report.queued == 1
    assert report.skipped_duplicate_merge == 1


async def test_dry_run_changes_nothing(silent_pipeline, seeded_db, settings):
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")])

    report = run_backfill(seeded_db, settings, dry_run=True)

    assert report.queued == 1
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 0
    assert seeded_db.scalar("SELECT class_source FROM jobs WHERE req_id='1'") == "seed"


async def test_tier_scope_leaves_other_tiers_alone(silent_pipeline, seeded_db, settings):
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")], tier="hot")
    await seed(pipe, seeded_db, "slowco", [posting(INTERN, "2")], tier="cold")

    report = run_backfill(seeded_db, settings, BackfillScope(tiers=("hot",)))

    assert report.queued == 1
    assert list(report.per_company) == ["Stripe"]
    assert seeded_db.scalar("SELECT class_source FROM jobs WHERE req_id='2'") == "seed"


async def test_company_scope(silent_pipeline, seeded_db, settings):
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")])
    await seed(pipe, seeded_db, "figma", [posting(INTERN, "2")])

    report = run_backfill(seeded_db, settings, BackfillScope(companies=("figma",)))

    assert report.queued == 1
    assert list(report.per_company) == ["Figma"]


async def test_match_only_leaves_review_reclassified_but_silent(
    silent_pipeline, seeded_db, settings
):
    pipe, _ = silent_pipeline
    await seed(
        pipe, seeded_db, "stripe", [posting(INTERN, "1"), posting("Sustainability Intern", "2")]
    )

    report = run_backfill(seeded_db, settings, BackfillScope(match_only=True))

    assert report.queued == 1
    assert queued_titles(seeded_db) == [INTERN]
    assert seeded_db.scalar("SELECT classification FROM jobs WHERE req_id='2'") == "review"


async def test_limit_caps_alerts_but_not_reclassification(
    silent_pipeline, seeded_db, settings
):
    pipe, _ = silent_pipeline
    await seed(
        pipe, seeded_db, "stripe", [posting(distinct_title(i), str(i)) for i in range(10)]
    )

    report = run_backfill(seeded_db, settings, BackfillScope(limit=3))

    assert report.queued == 3
    assert report.truncated_by_limit == 7
    assert report.reclassified == 10, "everything scanned is still triaged"
    assert seeded_db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE class_source = 'seed'", default=0
    ) == 0


async def test_postings_gone_from_the_board_are_skipped(
    silent_pipeline, seeded_db, settings
):
    """Default scope is what the source last actually returned, so a req that
    has since closed does not arrive as a fresh alert weeks later."""
    pipe, _ = silent_pipeline
    sid = await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")])

    # The source has polled successfully since; this job was not in that poll.
    seeded_db.execute(
        "UPDATE sources SET last_success_at = '2026-08-26T12:00:00Z' WHERE id = ?", (sid,)
    )
    seeded_db.execute("UPDATE jobs SET last_seen_at = '2026-08-01T12:00:00Z'")

    assert run_backfill(seeded_db, settings).queued == 0
    assert run_backfill(seeded_db, settings, BackfillScope(include_stale=True)).queued == 1


async def test_queued_rows_are_digests(silent_pipeline, seeded_db, settings):
    """A backfill is a bulk operation by definition; every row batches."""
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")], tier="hot")

    run_backfill(seeded_db, settings)

    assert seeded_db.scalar("SELECT kind FROM outbox LIMIT 1") == "digest"


async def test_backfilled_payload_matches_a_live_alert(silent_pipeline, seeded_db, settings):
    """Same shape as `Pipeline._payload` builds, plus the backfilled marker."""
    pipe, _ = silent_pipeline
    await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")])

    run_backfill(seeded_db, settings)

    payload = json.loads(seeded_db.scalar("SELECT payload FROM outbox LIMIT 1"))
    assert payload["type"] == "job"
    assert payload["company"] == "Stripe"
    assert payload["classification"] == "match"
    assert payload["backfilled"] is True
    assert payload["url"] and payload["merge_key"] and payload["ui_url"]


async def test_backfilled_alert_delivers_as_one_digest(silent_pipeline, seeded_db, settings):
    pipe, sender = silent_pipeline
    await seed(
        pipe, seeded_db, "stripe", [posting(distinct_title(i), str(i)) for i in range(5)]
    )

    run_backfill(seeded_db, settings)
    await pipe.outbox.flush()

    assert len(sender.messages) == 1, "five backfilled postings, one message"
    (embed,) = sender.messages[0]
    assert "5 new postings" in embed["title"]
    assert distinct_title(0) in embed["description"]


async def test_a_source_that_304s_is_not_mistaken_for_a_stale_board(
    silent_pipeline, seeded_db, settings
):
    """The live filter measures against `poll_log`, not `sources.last_success_at`.

    A 304 counts as a successful poll and bumps `last_success_at`, but `_ingest`
    never runs, so no job's `last_seen_at` moves. Measuring liveness against
    `last_success_at` therefore declares the whole board of every correctly
    caching source stale — which silently skipped 29 of 44 hot sources, and the
    3,903 seeded rows they held, the first time this ran for real.
    """
    pipe, _ = silent_pipeline
    sid = await seed(pipe, seeded_db, "stripe", [posting(INTERN, "1")])

    # A later poll answered 304: success recorded, nothing ingested.
    seeded_db.execute(
        "UPDATE sources SET last_success_at = '2026-08-26T14:00:00Z' WHERE id = ?", (sid,)
    )
    seeded_db.execute(
        "INSERT INTO poll_log(source_id, at, outcome) VALUES(?, '2026-08-26T14:00:00Z', ?)",
        (sid, "not_modified"),
    )

    assert run_backfill(seeded_db, settings).queued == 1
