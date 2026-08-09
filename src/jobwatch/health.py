"""Observability: heartbeat and drift detection (§12).

Drift detection is the only thing standing between you and a source that has
been quietly returning nothing since a redesign in October. The signature it
watches for is specific: a source that used to return postings and now returns
zero, *without* erroring. That looks identical to a company with no open
internships, so nothing else in the system can tell the difference.

Heartbeat rule: ping only when at least one source succeeded this tick. A
process that is alive but failing everything must not report healthy.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .db import Database, parse_ts, utcnow
from .logging_setup import get_logger
from .notify.outbox import Outbox

__all__ = ["Alarm", "HealthMonitor", "detect_drift"]

log = get_logger(__name__)

ALARM_SENT_PREFIX = "alarm.sent."
LAST_DRIFT_CHECK = "health.last_drift_check"

SEVERITY_ORDER = {"alarm": 0, "warn": 1, "ok": 2}


@dataclass(slots=True)
class Alarm:
    source_id: int
    source_label: str
    severity: str      # 'alarm' | 'warn'
    code: str          # 'zero_postings' | 'count_drop' | 'failing' | 'stuck_on_fallback'
    message: str
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.code}"


def detect_drift(db: Database, *, now: datetime | None = None) -> list[Alarm]:
    """Compare each source against its own recent history. Pure read, no writes."""
    moment = now or datetime.now(UTC)
    today = moment.strftime("%Y-%m-%d")
    alarms: list[Alarm] = []

    rows = db.query(
        """
        SELECT s.id, s.company_slug, s.adapter, s.consecutive_failures,
               s.using_fallback, s.last_success_at, s.fallback_adapter
        FROM sources s
        JOIN companies c ON c.slug = s.company_slug
        WHERE s.enabled = 1 AND c.enabled = 1
        """
    )

    for row in rows:
        label = f"{row['company_slug']}/{row['adapter']}"
        history = db.query(
            "SELECT day, max_count FROM source_baseline WHERE source_id = ? AND day < ? "
            "ORDER BY day DESC LIMIT 7",
            (row["id"], today),
        )
        counts = [h["max_count"] for h in history]
        baseline = int(statistics.median(counts)) if counts else 0

        today_row = db.one(
            "SELECT max_count FROM source_baseline WHERE source_id = ? AND day = ?",
            (row["id"], today),
        )
        today_count = today_row["max_count"] if today_row else None

        # The silent-breakage signature: it used to return postings, now zero,
        # and it is not erroring so nothing else would notice.
        if baseline > 5 and today_count == 0:
            alarms.append(
                Alarm(
                    row["id"], label, "alarm", "zero_postings",
                    f"{label} returned 0 postings today against a 7-day median of {baseline}",
                    "The endpoint still responds, so this is a parse or filter break, "
                    "not an outage. Run `jobwatch test` against it.",
                )
            )
        elif baseline > 5 and today_count is not None and today_count < baseline * 0.4:
            alarms.append(
                Alarm(
                    row["id"], label, "warn", "count_drop",
                    f"{label} dropped to {today_count} postings from a median of {baseline}",
                )
            )

        if row["consecutive_failures"] > 0:
            last_success = parse_ts(row["last_success_at"])
            if last_success is not None:
                broken_for = moment - last_success
                since = f"since {row['last_success_at']}"
            else:
                # A source that has never succeeded has no last_success to
                # measure from. Use its first recorded attempt instead —
                # otherwise a brand-new source that fails once is instantly
                # reported as "failing for over 24h", which is just wrong.
                first_attempt = parse_ts(
                    db.scalar(
                        "SELECT MIN(at) FROM poll_log WHERE source_id = ?", (row["id"],)
                    )
                )
                broken_for = (moment - first_attempt) if first_attempt else timedelta(0)
                since = "and has never succeeded"

            if broken_for >= timedelta(hours=24):
                hours = int(broken_for.total_seconds() // 3600)
                alarms.append(
                    Alarm(
                        row["id"], label, "alarm", "failing",
                        f"{label} has been failing for {hours}h "
                        f"({row['consecutive_failures']} consecutive failures) {since}",
                    )
                )

        if row["using_fallback"]:
            last_success = parse_ts(row["last_success_at"])
            if last_success and (moment - last_success) < timedelta(days=7):
                pass
            flipped_long_ago = last_success is None or (moment - last_success) >= timedelta(days=7)
            if flipped_long_ago:
                alarms.append(
                    Alarm(
                        row["id"], label, "warn", "stuck_on_fallback",
                        f"{label} has been on its {row['fallback_adapter']} fallback for over "
                        "7 days — the primary endpoint is probably gone for good",
                        "Update the registry entry rather than leaving it degraded.",
                    )
                )

    alarms.sort(key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.source_label))
    return alarms


class HealthMonitor:
    """Heartbeat pings and once-daily drift checks, both driven from the tick loop."""

    def __init__(
        self,
        db: Database,
        client: httpx.AsyncClient,
        outbox: Outbox,
        *,
        healthcheck_url: str | None = None,
        drift_interval_hours: float = 24.0,
    ) -> None:
        self.db = db
        self.client = client
        self.outbox = outbox
        self.healthcheck_url = healthcheck_url
        self.drift_interval = timedelta(hours=drift_interval_hours)

    async def after_tick(self, any_success: bool) -> None:
        if any_success:
            await self.ping_heartbeat()
        await self.maybe_check_drift()

    async def ping_heartbeat(self) -> None:
        if not self.healthcheck_url:
            return
        try:
            await self.client.get(self.healthcheck_url, timeout=10.0)
        except httpx.HTTPError as exc:
            # Losing the heartbeat is not worth a log line per tick.
            log.debug("heartbeat_failed", error=str(exc))

    async def maybe_check_drift(self) -> None:
        last = parse_ts(self.db.kv_get(LAST_DRIFT_CHECK))
        if last is not None and datetime.now(UTC) - last < self.drift_interval:
            return
        self.db.kv_set(LAST_DRIFT_CHECK, utcnow())
        self.check_drift()

    def check_drift(self) -> list[Alarm]:
        """Run detection and queue a Discord message for anything newly wrong."""
        alarms = detect_drift(self.db)
        active_keys = {a.key for a in alarms}

        for alarm in alarms:
            marker = ALARM_SENT_PREFIX + alarm.key
            if self.db.kv_get(marker):
                continue  # already reported; do not re-nag every day
            self.db.kv_set(marker, utcnow())
            self.outbox.enqueue_alarm(
                alarm.key,
                alarm.message,
                alarm.detail or "Open the health screen for the poll-log tail.",
                footer=f"jobwatch health · {alarm.severity}",
            )
            log.warning(
                "drift_alarm", source=alarm.source_label, code=alarm.code, message=alarm.message
            )

        # Clear markers for alarms that have resolved, so a recurrence reports again.
        for row in self.db.query(
            "SELECT key FROM kv WHERE key LIKE ?", (ALARM_SENT_PREFIX + "%",)
        ):
            key = row["key"][len(ALARM_SENT_PREFIX) :]
            if key not in active_keys:
                self.db.execute("DELETE FROM kv WHERE key = ?", (row["key"],))

        return alarms
