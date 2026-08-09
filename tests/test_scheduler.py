"""Scheduler: who is due, how often, and what a failure does (Phase 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import add_company, add_source, posting, set_postings
from jobwatch.db import utcnow
from jobwatch.http import backoff_delay
from jobwatch.scheduler import Scheduler


@pytest.fixture
def scheduler(make_pipeline, seeded_db, settings):
    pipe, sender = make_pipeline()
    return Scheduler(seeded_db, pipe, settings), pipe, sender


def touch(db, source_id: int, *, seconds_ago: float) -> None:
    when = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    db.execute(
        "UPDATE sources SET last_attempt_at = ? WHERE id = ?",
        (when.strftime("%Y-%m-%dT%H:%M:%SZ"), source_id),
    )


# ── due selection ─────────────────────────────────────────────────────────


def test_a_never_polled_source_is_due(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    add_source(seeded_db, "stripe")
    assert len(sched.due_sources()) == 1


def test_tier_intervals_are_respected(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "hotco", tier="hot")
    add_company(seeded_db, "warmco", tier="warm")
    hot = add_source(seeded_db, "hotco")
    warm = add_source(seeded_db, "warmco")

    touch(seeded_db, hot, seconds_ago=90)     # hot interval is 60s
    touch(seeded_db, warm, seconds_ago=90)    # warm interval is 600s

    due = {s.company_slug for s in sched.due_sources()}
    assert due == {"hotco"}


def test_min_interval_seconds_overrides_the_tier(scheduler, seeded_db):
    """A heavy Workday tenant must not run at the hot-tier cadence (§13.5)."""
    sched, _, _ = scheduler
    add_company(seeded_db, "bigco", tier="hot")
    sid = add_source(seeded_db, "bigco", "fake", {"min_interval_seconds": 600})
    touch(seeded_db, sid, seconds_ago=120)

    assert sched.due_sources() == []

    touch(seeded_db, sid, seconds_ago=700)
    assert len(sched.due_sources()) == 1


def test_seasonal_multiplier_stretches_intervals(seeded_db, make_pipeline, settings):
    """Phase 10 acceptance: intervals shift when the clock moves months."""
    settings.seasonal_multipliers = {"8": 1.0, "default": 3.0}
    pipe, _ = make_pipeline()
    sched = Scheduler(seeded_db, pipe, settings)

    august = datetime(2026, 8, 15, tzinfo=UTC)
    april = datetime(2026, 4, 15, tzinfo=UTC)
    assert settings.interval_for("hot", august) == 60
    assert settings.interval_for("hot", april) == 180

    add_company(seeded_db, "stripe", tier="hot")
    sid = add_source(seeded_db, "stripe")
    touch(seeded_db, sid, seconds_ago=90)

    assert len(sched.due_sources(august)) == 1
    assert sched.due_sources(april) == [], "April should still be inside the stretched interval"


def test_backoff_holds_a_source_back(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    future = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute("UPDATE sources SET backoff_until = ? WHERE id = ?", (future, sid))

    assert sched.due_sources() == []


def test_disabled_companies_and_sources_are_skipped(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "off")
    add_company(seeded_db, "on")
    off_source = add_source(seeded_db, "on", "fake1")
    add_source(seeded_db, "off")
    add_source(seeded_db, "on", "fake")

    seeded_db.execute("UPDATE companies SET enabled = 0 WHERE slug = 'off'")
    seeded_db.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (off_source,))

    due = sched.due_sources()
    assert len(due) == 1
    assert due[0].company_slug == "on" and due[0].adapter == "fake"


def test_sleep_length_never_exceeds_the_tick_interval(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    touch(seeded_db, sid, seconds_ago=0)
    assert 0.5 <= sched.seconds_until_next_due() <= sched.tick_interval


# ── ticks ─────────────────────────────────────────────────────────────────


async def test_a_tick_polls_every_due_source(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "a")
    add_company(seeded_db, "b")
    for slug in ("a", "b"):
        sid = add_source(seeded_db, slug, seeded=True)
        set_postings(seeded_db, sid, [posting("Software Engineer Intern", f"{slug}-1")])

    report = await sched.tick()

    assert report.due == 2
    assert report.polled == 2
    assert report.succeeded == 2
    assert report.alerted == 2  # different companies -> different merge keys


async def test_a_tick_with_nothing_due_does_no_work(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    touch(seeded_db, sid, seconds_ago=1)

    report = await sched.tick()
    assert report.due == 0 and report.polled == 0


async def test_a_crashing_company_does_not_stop_the_tick(scheduler, seeded_db, monkeypatch):
    sched, pipe, _ = scheduler
    add_company(seeded_db, "boom")
    add_company(seeded_db, "fine")
    add_source(seeded_db, "boom", seeded=True)
    good = add_source(seeded_db, "fine", seeded=True)
    set_postings(seeded_db, good, [posting("Software Engineer Intern", "1")])

    original = pipe.run_company

    async def sometimes_explode(sources):
        if sources and sources[0].company_slug == "boom":
            raise RuntimeError("unexpected")
        return await original(sources)

    monkeypatch.setattr(pipe, "run_company", sometimes_explode)

    report = await sched.tick()

    assert report.alerted == 1, "the healthy company was not polled"
    assert report.failed >= 1


async def test_restarting_produces_no_duplicate_alerts(make_pipeline, seeded_db, settings):
    """Phase 2 acceptance."""
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting("Software Engineer Intern", "1")])

    await Scheduler(seeded_db, pipe, settings).tick()
    seeded_db.execute("UPDATE sources SET last_attempt_at = NULL WHERE id = ?", (sid,))
    await Scheduler(seeded_db, pipe, settings).tick()  # "restart"

    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox", default=0) == 1


async def test_repeated_network_failure_backs_off_instead_of_looping(
    make_pipeline, seeded_db, settings
):
    pipe, _ = make_pipeline()
    sched = Scheduler(seeded_db, pipe, settings)
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", "fake", {"raise": "network down"}, seeded=True)

    for _ in range(3):
        seeded_db.execute("UPDATE sources SET backoff_until = NULL, last_attempt_at = NULL WHERE id = ?", (sid,))
        await sched.tick()

    row = seeded_db.one("SELECT consecutive_failures, backoff_until FROM sources WHERE id = ?", (sid,))
    assert row["consecutive_failures"] == 3
    assert row["backoff_until"] > utcnow(), "should be held back, not retried immediately"
    assert seeded_db.scalar(
        "SELECT COUNT(*) FROM poll_log WHERE outcome = 'error'", default=0
    ) == 3


# ── backoff maths ─────────────────────────────────────────────────────────


def test_backoff_grows_and_is_capped():
    import random

    rng = random.Random(0)
    delays = [backoff_delay(n, 30, 3600, rng=rng) for n in range(1, 10)]
    assert delays[0] < delays[3] < delays[6]
    assert all(d <= 3600 * 1.3 for d in delays)
    assert backoff_delay(0, 30, 3600) == 0.0


def test_backoff_is_jittered():
    """Thirty sources recovering from one outage must not retry in unison."""
    import random

    samples = {backoff_delay(3, 30, 3600, rng=random.Random(s)) for s in range(20)}
    assert len(samples) > 15


# ── startup housekeeping ──────────────────────────────────────────────────


def test_startup_prunes_old_poll_log_rows(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    old = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute(
        "INSERT INTO poll_log(source_id, at, outcome) VALUES(?,?,'ok')", (sid, old)
    )
    seeded_db.execute(
        "INSERT INTO poll_log(source_id, at, outcome) VALUES(?,?,'ok')", (sid, utcnow())
    )

    sched.startup_maintenance()

    assert seeded_db.scalar("SELECT COUNT(*) FROM poll_log", default=0) == 1


def test_startup_never_prunes_the_three_permanent_tables(scheduler, seeded_db):
    """§13.4."""
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    seeded_db.execute(
        "INSERT INTO jobs(dedup_key, merge_key, company_slug, source_id, title, "
        "normalized_title, locations, url, first_seen_at, last_seen_at, classification) "
        "VALUES('d','m','stripe',?,'t','t','[]','http://x','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z','match')",
        (sid,),
    )
    seeded_db.execute(
        "INSERT INTO alerted_merges(merge_key, company_slug, alerted_at, dedup_key) "
        "VALUES('m','stripe','2020-01-01T00:00:00Z','d')"
    )
    seeded_db.execute(
        "INSERT INTO title_verdicts(normalized_title, verdict, source, decided_at) "
        "VALUES('t','match','manual','2020-01-01T00:00:00Z')"
    )

    sched.startup_maintenance()

    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 1
    assert seeded_db.scalar("SELECT COUNT(*) FROM alerted_merges", default=0) == 1
    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 1


def test_startup_clears_an_absurdly_long_backoff(scheduler, seeded_db):
    sched, _, _ = scheduler
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    far_future = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute("UPDATE sources SET backoff_until = ? WHERE id = ?", (far_future, sid))

    sched.startup_maintenance()

    assert seeded_db.scalar("SELECT backoff_until FROM sources WHERE id = ?", (sid,)) is None


def test_source_config_survives_a_round_trip(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", "workday", {"tenant": "x", "min_interval_seconds": 300})
    from conftest import get_source

    source = get_source(seeded_db, sid)
    assert source.min_interval_seconds == 300
    assert json.loads(json.dumps(source.adapter_config))["tenant"] == "x"
