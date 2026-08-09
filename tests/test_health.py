"""Drift detection and heartbeat (§12, Phase 9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from conftest import RecordingSender, add_company, add_source, get_source, set_postings
from jobwatch.db import utcnow
from jobwatch.health import HealthMonitor, detect_drift
from jobwatch.notify.discord import DiscordChannel
from jobwatch.notify.outbox import Outbox


def baseline(db, source_id: int, counts: list[int], *, today: int | None = None) -> None:
    """Write `counts` as the previous days' maxima, newest last."""
    for offset, count in enumerate(reversed(counts), start=1):
        day = (datetime.now(UTC) - timedelta(days=offset)).strftime("%Y-%m-%d")
        db.execute(
            "INSERT OR REPLACE INTO source_baseline(source_id, day, max_count) VALUES(?,?,?)",
            (source_id, day, count),
        )
    if today is not None:
        db.execute(
            "INSERT OR REPLACE INTO source_baseline(source_id, day, max_count) VALUES(?,?,?)",
            (source_id, datetime.now(UTC).strftime("%Y-%m-%d"), today),
        )


@pytest.fixture
def monitor(seeded_db):
    sender = RecordingSender()
    outbox = Outbox(seeded_db, [DiscordChannel(sender)])  # type: ignore[list-item]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    return HealthMonitor(seeded_db, client, outbox, healthcheck_url="https://hc.test/ping"), sender


# ── drift ─────────────────────────────────────────────────────────────────


def test_a_source_dropping_to_zero_raises_an_alarm(seeded_db):
    """The silent-breakage signature: still responding, now returning nothing."""
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400, 410, 395, 402, 398, 405, 399], today=0)

    alarms = detect_drift(seeded_db)

    assert [a.code for a in alarms] == ["zero_postings"]
    assert alarms[0].severity == "alarm"
    assert "median of 400" in alarms[0].message


def test_zero_postings_is_not_an_alarm_when_the_baseline_is_tiny(seeded_db):
    add_company(seeded_db, "tiny")
    sid = add_source(seeded_db, "tiny")
    baseline(seeded_db, sid, [1, 0, 2, 1, 0], today=0)

    assert detect_drift(seeded_db) == []


def test_a_large_drop_warns(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400, 400, 400, 400, 400], today=50)

    alarms = detect_drift(seeded_db)

    assert [a.code for a in alarms] == ["count_drop"]
    assert alarms[0].severity == "warn"


def test_a_normal_day_raises_nothing(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400, 410, 395, 402], today=398)

    assert detect_drift(seeded_db) == []


def test_a_source_failing_for_a_day_raises_an_alarm(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    long_ago = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute(
        "UPDATE sources SET consecutive_failures = 9, last_success_at = ? WHERE id = ?",
        (long_ago, sid),
    )

    codes = [a.code for a in detect_drift(seeded_db)]
    assert "failing" in codes


def test_a_brand_new_source_that_fails_once_does_not_alarm(seeded_db):
    """It has never succeeded, but it has also only just started."""
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    seeded_db.execute(
        "UPDATE sources SET consecutive_failures = 1, last_success_at = NULL WHERE id = ?", (sid,)
    )
    seeded_db.execute(
        "INSERT INTO poll_log(source_id, at, outcome) VALUES(?,?,'error')", (sid, utcnow())
    )

    assert [a.code for a in detect_drift(seeded_db)] == []


def test_a_source_that_has_never_succeeded_for_a_day_does_alarm(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    long_ago = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute(
        "UPDATE sources SET consecutive_failures = 40, last_success_at = NULL WHERE id = ?", (sid,)
    )
    seeded_db.execute(
        "INSERT INTO poll_log(source_id, at, outcome) VALUES(?,?,'error')", (sid, long_ago)
    )

    codes = [a.code for a in detect_drift(seeded_db)]
    assert "failing" in codes


def test_a_source_failing_for_ten_minutes_does_not(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    recent = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute(
        "UPDATE sources SET consecutive_failures = 2, last_success_at = ? WHERE id = ?",
        (recent, sid),
    )

    assert [a.code for a in detect_drift(seeded_db)] == []


def test_a_source_stuck_on_its_fallback_warns(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", fallback_adapter="html")
    old = (datetime.now(UTC) - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seeded_db.execute(
        "UPDATE sources SET using_fallback = 1, last_success_at = ? WHERE id = ?", (old, sid)
    )

    codes = [a.code for a in detect_drift(seeded_db)]
    assert "stuck_on_fallback" in codes


def test_disabled_sources_are_not_alarmed_on(seeded_db):
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400] * 7, today=0)
    seeded_db.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (sid,))

    assert detect_drift(seeded_db) == []


def test_alarms_sort_most_severe_first(seeded_db):
    add_company(seeded_db, "a")
    add_company(seeded_db, "b")
    warned = add_source(seeded_db, "a")
    alarmed = add_source(seeded_db, "b")
    baseline(seeded_db, warned, [400] * 5, today=50)
    baseline(seeded_db, alarmed, [400] * 5, today=0)

    alarms = detect_drift(seeded_db)
    assert [a.severity for a in alarms] == ["alarm", "warn"]


# ── alarm delivery ────────────────────────────────────────────────────────


def test_an_alarm_is_queued_to_discord_once(monitor, seeded_db):
    health, _sender = monitor
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400] * 7, today=0)

    health.check_drift()
    health.check_drift()

    rows = seeded_db.query("SELECT * FROM outbox WHERE kind = 'alarm'")
    assert len(rows) == 1, "the same alarm was reported twice"


def test_a_resolved_alarm_can_fire_again_later(monitor, seeded_db):
    health, _ = monitor
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400] * 7, today=0)
    health.check_drift()

    # It recovers.
    seeded_db.execute(
        "UPDATE source_baseline SET max_count = 400 WHERE source_id = ? AND day = ?",
        (sid, datetime.now(UTC).strftime("%Y-%m-%d")),
    )
    health.check_drift()
    # Then breaks again.
    seeded_db.execute(
        "UPDATE source_baseline SET max_count = 0 WHERE source_id = ? AND day = ?",
        (sid, datetime.now(UTC).strftime("%Y-%m-%d")),
    )
    health.check_drift()

    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox WHERE kind='alarm'", default=0) == 2


async def test_a_stubbed_empty_adapter_alarms_within_one_cycle(
    make_pipeline, seeded_db, monitor
):
    """Phase 9 acceptance."""
    health, _ = monitor
    pipe, _ = make_pipeline()
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    baseline(seeded_db, sid, [400] * 7)

    set_postings(seeded_db, sid, [])           # the adapter now returns nothing
    await pipe.run_company([get_source(seeded_db, sid)])

    alarms = health.check_drift()
    assert [a.code for a in alarms] == ["zero_postings"]
    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox WHERE kind='alarm'", default=0) == 1


# ── heartbeat ─────────────────────────────────────────────────────────────


async def test_heartbeat_pings_only_when_something_succeeded(seeded_db):
    """§12: alive but failing everything must not report healthy."""
    pings: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pings.append(str(request.url))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    health = HealthMonitor(
        seeded_db, client, Outbox(seeded_db, [DiscordChannel(RecordingSender())]),  # type: ignore[list-item]
        healthcheck_url="https://hc.test/ping",
    )
    try:
        await health.after_tick(any_success=False)
        assert pings == []

        await health.after_tick(any_success=True)
        assert pings == ["https://hc.test/ping"]
    finally:
        await client.aclose()


async def test_heartbeat_failure_is_not_fatal(seeded_db):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    health = HealthMonitor(
        seeded_db, client, Outbox(seeded_db, [DiscordChannel(RecordingSender())]),  # type: ignore[list-item]
        healthcheck_url="https://hc.test/ping",
    )
    try:
        await health.after_tick(any_success=True)  # must not raise
    finally:
        await client.aclose()


async def test_drift_check_runs_at_most_once_per_interval(monitor, seeded_db):
    health, _ = monitor
    add_company(seeded_db, "stripe")
    sid = add_source(seeded_db, "stripe")
    baseline(seeded_db, sid, [400] * 7, today=0)

    await health.maybe_check_drift()
    seeded_db.execute("DELETE FROM kv WHERE key LIKE 'alarm.sent.%'")
    await health.maybe_check_drift()

    assert seeded_db.scalar("SELECT COUNT(*) FROM outbox WHERE kind='alarm'", default=0) == 1
    assert seeded_db.kv_get("health.last_drift_check") <= utcnow()
