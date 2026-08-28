"""Outbox: exactly-once delivery, retry, and the failure surface (§9, Phase 3)."""

from __future__ import annotations

import json

import pytest

from conftest import RecordingSender, add_company, add_source, get_source, posting, set_postings
from jobwatch.notify.base import DeliveryError
from jobwatch.notify.discord import (
    DiscordChannel,
    DiscordError,
    DiscordSender,
    _chunk_embeds,
    _embed_cost,
    build_alarm_embed,
    build_digest_embed,
    build_digest_embeds,
    build_job_embed,
    humanize_age,
)
from jobwatch.notify.outbox import Outbox

PAYLOAD = {
    "type": "job",
    "company": "Stripe",
    "company_slug": "stripe",
    "title": "Software Engineering Intern, Summer 2027",
    "url": "https://careers.stripe.com/jobs/1",
    "locations": ["San Francisco", "Seattle", "NYC"],
    "classification": "match",
    "detected_at": "2026-08-02T12:00:00Z",
    "ui_url": "http://127.0.0.1:8080/?q=abc",
}


@pytest.fixture
def outbox(seeded_db):
    sender = RecordingSender()
    return Outbox(seeded_db, [DiscordChannel(sender)], max_attempts=3), sender  # type: ignore[list-item]


# ── delivery ──────────────────────────────────────────────────────────────


async def test_a_queued_row_is_delivered_once_and_marked_sent(outbox, seeded_db):
    box, sender = outbox
    box.enqueue("mk-1", PAYLOAD)

    sent, failed = await box.flush()

    assert (sent, failed) == (1, 0)
    assert len(sender.messages) == 1
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "sent"

    await box.flush()  # a second pass must not re-send
    assert len(sender.messages) == 1


async def test_delivery_survives_being_killed_mid_send(outbox, seeded_db):
    """Phase 3 acceptance: a crash between enqueue and send loses nothing."""
    box, sender = outbox
    box.enqueue("mk-1", PAYLOAD)

    # The process dies here — the row is committed but never sent.
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "queued"

    restarted = Outbox(seeded_db, [DiscordChannel(sender)])  # type: ignore[list-item]
    await restarted.flush()
    await restarted.flush()

    assert len(sender.messages) == 1, "restart delivered it more than once"


async def test_a_transient_failure_is_retried_not_dropped(seeded_db):
    sender = RecordingSender(fail_times=1)
    box = Outbox(seeded_db, [DiscordChannel(sender)], max_attempts=3)  # type: ignore[list-item]
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()
    row = seeded_db.one("SELECT status, attempts, next_attempt_at FROM outbox WHERE id = 1")
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None

    # Backoff would hold it; clear it to simulate the wait elapsing.
    seeded_db.execute("UPDATE outbox SET next_attempt_at = NULL WHERE id = 1")
    await box.flush()
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "sent"


async def test_repeated_failure_ends_as_a_failed_row_not_a_crash(seeded_db):
    sender = RecordingSender(fail_times=99)
    box = Outbox(seeded_db, [DiscordChannel(sender)], max_attempts=3)  # type: ignore[list-item]
    box.enqueue("mk-1", PAYLOAD)

    for _ in range(3):
        seeded_db.execute("UPDATE outbox SET next_attempt_at = NULL WHERE id = 1")
        await box.flush()

    row = seeded_db.one("SELECT status, attempts, last_error FROM outbox WHERE id = 1")
    assert row["status"] == "failed"
    assert row["attempts"] == 3
    assert row["last_error"]


async def test_a_deleted_webhook_fails_immediately_rather_than_retrying_into_a_wall(seeded_db):
    """Phase 3 acceptance: an invalid webhook URL produces a failed row."""
    sender = RecordingSender(fatal=True)
    box = Outbox(seeded_db, [DiscordChannel(sender)], max_attempts=5)  # type: ignore[list-item]
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()

    row = seeded_db.one("SELECT status, attempts FROM outbox WHERE id = 1")
    assert row["status"] == "failed"
    assert row["attempts"] == 1, "a permanent rejection should not burn all five attempts"


async def test_flush_never_raises(seeded_db):
    class Exploding:
        configured = True

        async def send_embeds(self, embeds, *, content=None):
            raise RuntimeError("something completely unexpected")

    box = Outbox(seeded_db, [DiscordChannel(Exploding())])  # type: ignore[list-item]
    box.enqueue("mk-1", PAYLOAD)
    await box.flush()  # must not propagate
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "queued"


async def test_replay_requeues_a_failed_row(seeded_db):
    sender = RecordingSender(fatal=True)
    box = Outbox(seeded_db, [DiscordChannel(sender)], max_attempts=1)  # type: ignore[list-item]
    box.enqueue("mk-1", PAYLOAD)
    await box.flush()
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "failed"

    box.channels = [DiscordChannel(RecordingSender())]  # type: ignore[list-item]
    assert box.replay(1) is True
    await box.flush()

    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "sent"


async def test_counts_reports_each_status(seeded_db):
    box = Outbox(seeded_db, [DiscordChannel(RecordingSender())])  # type: ignore[list-item]
    box.enqueue("a", PAYLOAD)
    box.enqueue("b", PAYLOAD)
    await box.flush()
    box.enqueue("c", PAYLOAD)

    assert box.counts() == {"queued": 1, "sent": 2, "failed": 0}


# ── more than one channel ─────────────────────────────────────────────────


class FakeChannel:
    """A channel that records payloads and can be told to fail."""

    def __init__(self, name: str, *, fail_times: int = 0, fatal: bool = False, configured: bool = True):
        self.name = name
        self.batches: list[list[dict]] = []
        self.fail_times = fail_times
        self.fatal = fatal
        self.attempts = 0
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    async def send(self, payloads, *, kind="job"):
        self.attempts += 1
        if self.fatal:
            raise DeliveryError(f"{self.name} is permanently broken", retryable=False)
        if self.attempts <= self.fail_times:
            raise DeliveryError(f"{self.name} blipped")
        self.batches.append(list(payloads))


async def test_an_alert_reaches_every_configured_channel(seeded_db):
    discord, email = FakeChannel("discord"), FakeChannel("email")
    box = Outbox(seeded_db, [discord, email])
    box.enqueue("mk-1", PAYLOAD)

    sent, failed = await box.flush()

    assert (sent, failed) == (1, 0)
    assert len(discord.batches) == 1
    assert len(email.batches) == 1
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "sent"


async def test_a_row_is_not_sent_until_every_channel_has_taken_it(seeded_db):
    discord, email = FakeChannel("discord"), FakeChannel("email", fail_times=1)
    box = Outbox(seeded_db, [discord, email], max_attempts=3)
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()

    row = seeded_db.one("SELECT status, attempts, last_error FROM outbox WHERE id = 1")
    assert row["status"] == "queued", "email has not had it yet"
    assert row["attempts"] == 1
    assert "email" in row["last_error"]


async def test_a_retry_does_not_re_send_to_the_channel_that_already_succeeded(seeded_db):
    """Without per-channel bookkeeping, a flaky SMTP server means a duplicate
    Discord ping on every attempt — the exact failure the outbox exists to stop."""
    discord, email = FakeChannel("discord"), FakeChannel("email", fail_times=1)
    box = Outbox(seeded_db, [discord, email], max_attempts=3)
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()
    seeded_db.execute("UPDATE outbox SET next_attempt_at = NULL WHERE id = 1")
    await box.flush()

    assert len(discord.batches) == 1, "discord was sent the same alert twice"
    assert len(email.batches) == 1
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "sent"


async def test_the_row_records_which_channels_have_taken_it(seeded_db):
    discord, email = FakeChannel("discord"), FakeChannel("email", fail_times=1)
    box = Outbox(seeded_db, [discord, email])
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()

    assert json.loads(seeded_db.scalar("SELECT delivered FROM outbox WHERE id = 1")) == ["discord"]


async def test_an_unconfigured_channel_is_skipped_rather_than_failing_the_row(seeded_db):
    """Email being unconfigured is the normal state, not a delivery failure."""
    discord = FakeChannel("discord")
    email = FakeChannel("email", configured=False)
    box = Outbox(seeded_db, [discord, email])
    box.enqueue("mk-1", PAYLOAD)

    sent, failed = await box.flush()

    assert (sent, failed) == (1, 0)
    assert email.attempts == 0
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "sent"


async def test_with_nothing_configured_the_row_fails_and_says_what_to_set(seeded_db):
    box = Outbox(seeded_db, [FakeChannel("discord", configured=False)])
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()

    row = seeded_db.one("SELECT status, last_error FROM outbox WHERE id = 1")
    assert row["status"] == "failed"
    assert "DISCORD_WEBHOOK_URL" in row["last_error"]
    assert "SMTP_HOST" in row["last_error"]


async def test_one_channel_going_permanently_wrong_still_lets_the_other_deliver(seeded_db):
    discord, email = FakeChannel("discord", fatal=True), FakeChannel("email")
    box = Outbox(seeded_db, [discord, email], max_attempts=3)
    box.enqueue("mk-1", PAYLOAD)

    await box.flush()

    assert len(email.batches) == 1, "a dead webhook must not cost the email"
    row = seeded_db.one("SELECT status, delivered FROM outbox WHERE id = 1")
    assert row["status"] == "failed", "discord will never take it"
    assert json.loads(row["delivered"]) == ["email"]


async def test_replay_clears_the_delivery_record_so_it_goes_out_again(seeded_db):
    discord, email = FakeChannel("discord"), FakeChannel("email")
    box = Outbox(seeded_db, [discord, email])
    box.enqueue("mk-1", PAYLOAD)
    await box.flush()

    box.replay(1)
    await box.flush()

    assert len(discord.batches) == 2, "replay means send it again, to everything"
    assert len(email.batches) == 2


async def test_channel_status_reports_each_channel_for_the_ui(seeded_db):
    box = Outbox(seeded_db, [FakeChannel("discord"), FakeChannel("email", configured=False)])

    assert box.channel_status() == [
        {"name": "discord", "configured": True},
        {"name": "email", "configured": False},
    ]
    assert [c.name for c in box.live_channels] == ["discord"]


async def test_a_digest_reaches_every_channel_as_one_batch(seeded_db):
    discord, email = FakeChannel("discord"), FakeChannel("email")
    box = Outbox(seeded_db, [discord, email], digest_interval_minutes=0)
    for i in range(3):
        box.enqueue(f"mk-{i}", {**PAYLOAD, "title": f"Intern {i}"}, kind="digest")

    await box.flush()

    assert len(discord.batches) == 1 and len(discord.batches[0]) == 3
    assert len(email.batches) == 1 and len(email.batches[0]) == 3


# ── digests ───────────────────────────────────────────────────────────────


async def test_digest_rows_batch_into_one_message(seeded_db):
    sender = RecordingSender()
    box = Outbox(seeded_db, [DiscordChannel(sender)], digest_interval_minutes=0)  # type: ignore[list-item]
    for i in range(5):
        box.enqueue(f"mk-{i}", {**PAYLOAD, "title": f"Intern {i}"}, kind="digest")

    await box.flush()

    assert len(sender.messages) == 1, "a digest should be one message, not five"
    assert seeded_db.scalar(
        "SELECT COUNT(*) FROM outbox WHERE status = 'sent'", default=0
    ) == 5


async def test_digest_waits_for_its_interval(seeded_db):
    from jobwatch.db import utcnow

    sender = RecordingSender()
    box = Outbox(seeded_db, [DiscordChannel(sender)], digest_interval_minutes=30)  # type: ignore[list-item]
    seeded_db.kv_set("outbox.last_digest_at", utcnow())
    box.enqueue("mk-1", PAYLOAD, kind="digest")

    await box.flush()

    assert sender.messages == []
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "queued"


async def test_immediate_rows_are_not_held_by_the_digest_timer(seeded_db):
    from jobwatch.db import utcnow

    sender = RecordingSender()
    box = Outbox(seeded_db, [DiscordChannel(sender)], digest_interval_minutes=30)  # type: ignore[list-item]
    seeded_db.kv_set("outbox.last_digest_at", utcnow())
    box.enqueue("mk-hot", PAYLOAD, kind="job")

    await box.flush()

    assert len(sender.messages) == 1


# ── embed formatting ──────────────────────────────────────────────────────


def test_job_embed_carries_the_apply_link_and_the_elapsed_time():
    embed = build_job_embed(PAYLOAD)
    assert embed["url"] == PAYLOAD["url"]
    assert "Stripe" in embed["title"]
    assert "careers.stripe.com" in embed["description"]
    assert "San Francisco · Seattle · NYC" in embed["description"]
    assert embed["color"] == 0x5EC1A0


def test_review_embed_is_amber_and_says_so():
    embed = build_job_embed({**PAYLOAD, "classification": "review"})
    assert embed["color"] == 0xF5A524
    assert "review" in embed["footer"]["text"].lower()


def test_digest_embed_lists_every_posting():
    embed = build_digest_embed([{**PAYLOAD, "title": f"Intern {i}"} for i in range(3)])
    assert "3 new postings" in embed["title"]
    for i in range(3):
        assert f"Intern {i}" in embed["description"]


def test_a_bulk_digest_spans_embeds_instead_of_dropping_postings():
    """The outbox pages 200 rows per flush; a single embed showed 25 of them."""
    payloads = [{**PAYLOAD, "title": f"Software Engineer Intern Team {i}"} for i in range(200)]

    embeds = build_digest_embeds(payloads)

    assert len(embeds) > 1, "a full page cannot fit in one embed"
    body = "".join(e["description"] for e in embeds)
    for i in range(200):
        assert f"Team {i}]" in body, f"posting {i} was dropped"
    assert "200 new postings" in embeds[0]["title"]
    assert "more — see the feed" not in body, "nothing needed truncating"


@pytest.mark.parametrize("count", [1, 2, 26, 60, 200, 500, 4000])
def test_every_digest_message_stays_within_discord_limits(count):
    """Discord enforces 10 embeds AND 6000 characters per message, and the
    second one 400s the whole message rather than trimming it. Budgeting per
    embed and sending ten of them is exactly how this broke in production."""
    embeds = build_digest_embeds([{**PAYLOAD, "title": f"Intern {i}"} for i in range(count)])

    for message in _chunk_embeds(embeds):
        assert len(message) <= 10
        assert sum(_embed_cost(e) for e in message) <= 6000
        for embed in message:
            assert len(embed["description"]) <= 4096


def test_a_digest_too_large_even_to_page_names_the_remainder():
    embeds = build_digest_embeds([{**PAYLOAD, "title": f"Intern {i}"} for i in range(4000)])

    assert len(embeds) == 30
    assert "more — see the feed" in embeds[-1]["description"]
    assert "4000 new postings" in embeds[0]["title"]


def test_a_small_digest_is_still_one_embed():
    embeds = build_digest_embeds([{**PAYLOAD, "title": f"Intern {i}"} for i in range(3)])

    assert len(embeds) == 1
    assert "3 new postings" in embeds[0]["title"]


def test_alarm_embed_is_visually_distinct():
    embed = build_alarm_embed({"title": "stripe/greenhouse returned 0 postings", "body": "check it"})
    assert embed["color"] == 0xE5484D


def test_long_digests_are_truncated_not_dropped():
    embed = build_digest_embed([{**PAYLOAD, "title": f"Intern {i}"} for i in range(60)])
    assert len(embed["description"]) <= 4000
    assert "35 more" in embed["description"]


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2026-08-02T11:59:46Z", "14s"),
        ("2026-08-02T11:57:00Z", "3m"),
        ("2026-08-02T10:00:00Z", "2h"),
        ("2026-07-29T12:00:00Z", "4d"),
        (None, "—"),
    ],
)
def test_humanize_age(iso, expected):
    from datetime import UTC, datetime

    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    assert humanize_age(iso, now=now) == expected


async def test_sender_without_a_webhook_fails_permanently(seeded_db):
    import httpx

    sender = DiscordSender(client=httpx.AsyncClient(), webhook_url=None)
    with pytest.raises(DiscordError) as exc:
        await sender.send_embeds([{"title": "x"}])
    assert exc.value.retryable is False
    await sender.client.aclose()


async def test_sender_throttles_to_the_configured_rate():
    import time

    import httpx

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(204))
    )
    sender = DiscordSender(client=client, webhook_url="https://discord.test/hook", max_per_second=20)
    try:
        started = time.monotonic()
        for _ in range(4):
            await sender.send_embeds([{"title": "x"}])
        elapsed = time.monotonic() - started
    finally:
        await client.aclose()
    # 4 messages at 20/s cannot complete faster than ~150ms of enforced gaps.
    assert elapsed >= 0.12


# ── integration with the pipeline ─────────────────────────────────────────


async def test_payload_round_trips_from_pipeline_to_embed(make_pipeline, seeded_db):
    pipe, sender = make_pipeline()
    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(
        seeded_db, sid,
        [posting("Software Engineer Intern", "1", locations=["Austin, TX", "Remote"])],
    )

    await pipe.run_company([get_source(seeded_db, sid)])
    await pipe.outbox.flush()

    assert len(sender.messages) == 1
    embed = sender.messages[0][0]
    assert "Stripe" in embed["title"]
    assert "Software Engineer Intern" in embed["title"]
    assert "Austin, TX" in embed["description"]

    stored = json.loads(seeded_db.scalar("SELECT payload FROM outbox LIMIT 1"))
    assert stored["merge_key"]
    assert stored["ui_url"].startswith("http")
