"""The durable notification queue (§9, Phase 3).

Nothing sends directly from the pipeline. Every alert is a committed row first,
which is what makes delivery survive being killed mid-send: on restart the row
is still `queued`, and it goes out exactly once.

Exactly-once, precisely: `alerted_merges` is written in the *same transaction*
as the outbox row, so a crash can never produce a merge marked alerted with no
queued message, nor a message with no marker. The send itself is at-least-once
at the transport layer — a response lost in flight is re-sent — which is the
correct trade for this system: a rare duplicate beats a silently dropped
internship.

**Channels.** A row is delivered to every configured channel: Discord, email, or
both. Which channels have already accepted a row is recorded on the row itself,
so a retry after a partial failure re-sends only to the channel that failed —
without that, a flaky SMTP server would mean a duplicate Discord ping on every
attempt. A row is `sent` only once every configured channel has taken it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..db import Database, utcnow
from ..logging_setup import get_logger
from .base import Channel, DeliveryError

__all__ = ["Outbox", "OutboxItem"]

log = get_logger(__name__)

LAST_DIGEST_KEY = "outbox.last_digest_at"

NO_CHANNELS = (
    "no notification channel configured — set DISCORD_WEBHOOK_URL, or SMTP_HOST "
    "and EMAIL_TO, in .env and restart"
)


@dataclass(slots=True)
class OutboxItem:
    id: int
    merge_key: str
    payload: dict[str, Any]
    attempts: int
    status: str
    kind: str
    created_at: str
    last_error: str | None = None
    # Channel names that have already accepted this row.
    delivered: set[str] = field(default_factory=set)


class Outbox:
    """Enqueue, deliver, retry. Owns every write to the `outbox` table."""

    def __init__(
        self,
        db: Database,
        channels: Sequence[Channel] = (),
        *,
        max_attempts: int = 5,
        digest_interval_minutes: int = 30,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.channels: list[Channel] = list(channels)
        self.max_attempts = max_attempts
        self.digest_interval = timedelta(minutes=digest_interval_minutes)
        self.dry_run = dry_run

    # -- channels ----------------------------------------------------------

    @property
    def live_channels(self) -> list[Channel]:
        """Channels with credentials. The rest are not failures, just absent."""
        return [channel for channel in self.channels if channel.configured]

    def channel_status(self) -> list[dict[str, Any]]:
        """(name, configured) for every known channel. Backs the UI banner."""
        return [{"name": c.name, "configured": bool(c.configured)} for c in self.channels]

    # -- enqueue -----------------------------------------------------------

    def enqueue(self, merge_key: str, payload: dict[str, Any], *, kind: str = "job") -> int:
        """Queue one notification. Call inside the caller's transaction."""
        cur = self.db.execute(
            "INSERT INTO outbox(merge_key, payload, status, created_at, kind, next_attempt_at) "
            "VALUES(?,?,'queued',?,?,?)",
            (merge_key, json.dumps(payload), utcnow(), kind, utcnow()),
        )
        return int(cur.lastrowid or 0)

    def enqueue_alarm(self, key: str, title: str, body: str, footer: str = "jobwatch health") -> int:
        return self.enqueue(
            f"alarm:{key}",
            {"type": "alarm", "title": title, "body": body, "footer": footer},
            kind="alarm",
        )

    # -- delivery ----------------------------------------------------------

    async def flush(self) -> tuple[int, int]:
        """Deliver everything due. Returns (sent, failed).

        Never raises: a delivery problem is a row status, not an exception that
        stops the scheduler.
        """
        sent = failed = 0
        try:
            sent_now, failed_now = await self._flush_immediate()
            sent += sent_now
            failed += failed_now
            sent_now, failed_now = await self._flush_digest()
            sent += sent_now
            failed += failed_now
        # A delivery problem is a row status, never an exception that stops the scheduler.
        except Exception as exc:
            log.error("outbox_flush_failed", error=str(exc), exc_info=True)
        return sent, failed

    async def _flush_immediate(self) -> tuple[int, int]:
        rows = self.db.query(
            "SELECT * FROM outbox WHERE status = 'queued' AND kind != 'digest' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id LIMIT 100",
            (utcnow(),),
        )
        sent = failed = 0
        for row in rows:
            item = _to_item(row)
            ok = await self._deliver([item], item.kind)
            if ok:
                sent += 1
            elif self.db.scalar("SELECT status FROM outbox WHERE id = ?", (item.id,)) == "failed":
                failed += 1
        return sent, failed

    async def _flush_digest(self) -> tuple[int, int]:
        """Warm and cold tiers accumulate into one message on an interval (§9)."""
        rows = self.db.query(
            "SELECT * FROM outbox WHERE status = 'queued' AND kind = 'digest' ORDER BY id LIMIT 200"
        )
        if not rows:
            return 0, 0

        last = self.db.kv_get(LAST_DIGEST_KEY)
        if last:
            try:
                due_at = datetime.fromisoformat(last.replace("Z", "+00:00")) + self.digest_interval
                if datetime.now(UTC) < due_at:
                    return 0, 0
            except ValueError:
                pass

        items = [_to_item(r) for r in rows]
        ok = await self._deliver(items, "digest")
        self.db.kv_set(LAST_DIGEST_KEY, utcnow())
        return (len(items), 0) if ok else (0, sum(1 for i in items if i.attempts + 1 >= self.max_attempts))

    async def _deliver(self, items: list[OutboxItem], kind: str) -> bool:
        """Hand this batch to every channel that has not already taken it."""
        if self.dry_run:
            for item in items:
                log.info(
                    "dry_run_would_send",
                    merge_key=item.merge_key,
                    title=item.payload.get("title"),
                    company=item.payload.get("company"),
                    classification=item.payload.get("classification"),
                    url=item.payload.get("url"),
                    channels=[c.name for c in self.live_channels] or ["none configured"],
                )
            self._mark_sent(items)
            return True

        channels = self.live_channels
        if not channels:
            self._mark_attempt_failed(items, NO_CHANNELS, retryable=False)
            return False

        payloads = [item.payload for item in items]
        errors: list[str] = []
        retryable = False

        for channel in channels:
            # Already accepted on an earlier attempt: re-sending would duplicate.
            if all(channel.name in item.delivered for item in items):
                continue
            try:
                await channel.send(payloads, kind=kind)
            except DeliveryError as exc:
                errors.append(f"{channel.name}: {exc}")
                retryable = retryable or exc.retryable
            # Never let one channel's surprise kill the loop or the other channel.
            except Exception as exc:
                errors.append(f"{channel.name}: {type(exc).__name__}: {exc}")
                retryable = True
            else:
                self._mark_delivered(items, channel.name)

        if errors:
            # One bookkeeping write per attempt, not per channel: `attempts` counts
            # passes over the row, and a mixed result retries so the failed channel
            # gets another go. The channel that succeeded is skipped above.
            self._mark_attempt_failed(items, "; ".join(errors), retryable=retryable)
            return False

        self._mark_sent(items)
        return True

    # -- row bookkeeping ---------------------------------------------------

    def _mark_delivered(self, items: list[OutboxItem], channel: str) -> None:
        with self.db.tx() as conn:
            for item in items:
                item.delivered.add(channel)
                conn.execute(
                    "UPDATE outbox SET delivered=? WHERE id=?",
                    (json.dumps(sorted(item.delivered)), item.id),
                )

    def _mark_sent(self, items: list[OutboxItem]) -> None:
        now = utcnow()
        with self.db.tx() as conn:
            for item in items:
                conn.execute(
                    "UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1, "
                    "last_error=NULL WHERE id=?",
                    (now, item.id),
                )

    def _mark_attempt_failed(self, items: list[OutboxItem], error: str, *, retryable: bool) -> None:
        with self.db.tx() as conn:
            for item in items:
                attempts = item.attempts + 1
                exhausted = (not retryable) or attempts >= self.max_attempts
                if exhausted:
                    conn.execute(
                        "UPDATE outbox SET status='failed', attempts=?, last_error=? WHERE id=?",
                        (attempts, error[:500], item.id),
                    )
                    log.error(
                        "outbox_row_failed",
                        outbox_id=item.id,
                        attempts=attempts,
                        retryable=retryable,
                        delivered=sorted(item.delivered),
                        error=error[:200],
                    )
                else:
                    delay = min(2**attempts * 5, 900)
                    next_at = (datetime.now(UTC) + timedelta(seconds=delay)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    conn.execute(
                        "UPDATE outbox SET attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                        (attempts, error[:500], next_at, item.id),
                    )
                    log.warning(
                        "outbox_retry_scheduled",
                        outbox_id=item.id,
                        attempts=attempts,
                        retry_in_s=delay,
                        delivered=sorted(item.delivered),
                        error=error[:200],
                    )

    # -- operations --------------------------------------------------------

    def replay(self, outbox_id: int) -> bool:
        """Re-queue a failed or sent row. Backs the UI replay button.

        `delivered` is cleared: replay means send it again, to everything.
        """
        cur = self.db.execute(
            "UPDATE outbox SET status='queued', attempts=0, last_error=NULL, "
            "next_attempt_at=?, sent_at=NULL, delivered=NULL WHERE id=?",
            (utcnow(), outbox_id),
        )
        return bool(cur.rowcount)

    def replay_merge(self, merge_key: str) -> int:
        cur = self.db.execute(
            "UPDATE outbox SET status='queued', attempts=0, last_error=NULL, "
            "next_attempt_at=?, sent_at=NULL, delivered=NULL WHERE merge_key=?",
            (utcnow(), merge_key),
        )
        return cur.rowcount or 0

    def counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        counts = {r["status"]: r["n"] for r in rows}
        return {k: counts.get(k, 0) for k in ("queued", "sent", "failed")}

    async def run_forever(self, interval_seconds: float = 2.0) -> None:
        """Background delivery loop. One task, cancelled on shutdown."""
        log.info(
            "outbox_worker_started",
            interval_s=interval_seconds,
            dry_run=self.dry_run,
            channels=[c.name for c in self.live_channels],
        )
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("outbox_worker_error", error=str(exc), exc_info=True)
            await asyncio.sleep(interval_seconds)


def _to_item(row: Any) -> OutboxItem:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return OutboxItem(
        id=row["id"],
        merge_key=row["merge_key"],
        payload=payload if isinstance(payload, dict) else {},
        attempts=row["attempts"],
        status=row["status"],
        # `.keys()` is required: sqlite3.Row.__contains__ tests values, not keys.
        kind=row["kind"] if "kind" in row.keys() else "job",  # noqa: SIM118
        created_at=row["created_at"],
        last_error=row["last_error"],
        delivered=_delivered(row),
    )


def _delivered(row: Any) -> set[str]:
    if "delivered" not in row.keys():  # noqa: SIM118
        return set()
    try:
        value = json.loads(row["delivered"] or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(name) for name in value} if isinstance(value, list) else set()
