"""The scheduler (§7, Phase 2): who is due, how often, and how many at once.

Hand-rolled rather than cron-driven because the interesting logic is per-source
state — backoff, fallback flips, conditional-request headers — and none of that
fits a fixed schedule.

Sources are grouped by company before dispatch. That grouping is what makes
cross-source attribution correct: both of a company's sources are polled in the
same pass, so the alert can link to the priority-1 apply path (§5).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Settings
from .db import Database, parse_ts, utcnow
from .health import HealthMonitor
from .logging_setup import get_logger
from .models import Source
from .pipeline import CompanyReport, Pipeline

__all__ = ["Scheduler", "TickReport"]

log = get_logger(__name__)

SOURCE_SELECT = """
SELECT s.*, c.tier AS tier, c.display_name AS display_name,
       c.enabled AS company_enabled
FROM sources s
JOIN companies c ON c.slug = s.company_slug
"""


@dataclass(slots=True)
class TickReport:
    started_at: str
    due: int = 0
    polled: int = 0
    succeeded: int = 0
    failed: int = 0
    alerted: int = 0
    duration_ms: int = 0
    reports: list[CompanyReport] = field(default_factory=list)

    @property
    def any_success(self) -> bool:
        return self.succeeded > 0


class Scheduler:
    def __init__(
        self,
        db: Database,
        pipeline: Pipeline,
        settings: Settings,
        *,
        health: HealthMonitor | None = None,
        tick_interval: float = 5.0,
    ) -> None:
        self.db = db
        self.pipeline = pipeline
        self.settings = settings
        self.health = health
        self.tick_interval = tick_interval
        self._running = False

    # ── due-source selection ──────────────────────────────────────────────

    def all_sources(self) -> list[Source]:
        return [Source.from_row(r) for r in self.db.query(SOURCE_SELECT + " ORDER BY s.id")]

    def due_sources(self, now: datetime | None = None) -> list[Source]:
        """Sources whose interval has elapsed and whose backoff has expired."""
        moment = now or datetime.now(UTC)
        now_iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = self.db.query(
            SOURCE_SELECT
            + " WHERE s.enabled = 1 AND c.enabled = 1"
            "   AND (s.backoff_until IS NULL OR s.backoff_until <= ?)"
            " ORDER BY c.tier, s.priority, s.id",
            (now_iso,),
        )

        due: list[Source] = []
        for row in rows:
            source = Source.from_row(row)
            interval = max(
                self.settings.interval_for(source.tier, moment),
                source.min_interval_seconds,
            )
            last = parse_ts(source.last_attempt_at)
            if last is None or (moment - last) >= timedelta(seconds=interval):
                due.append(source)
        return due

    def seconds_until_next_due(self, now: datetime | None = None) -> float:
        """How long the loop may safely sleep. Used to keep idle CPU near zero."""
        moment = now or datetime.now(UTC)
        soonest = self.tick_interval
        for row in self.db.query(SOURCE_SELECT + " WHERE s.enabled = 1 AND c.enabled = 1"):
            source = Source.from_row(row)
            interval = max(
                self.settings.interval_for(source.tier, moment),
                source.min_interval_seconds,
            )
            last = parse_ts(source.last_attempt_at)
            ready_at = moment if last is None else last + timedelta(seconds=interval)
            backoff = parse_ts(source.backoff_until)
            if backoff and backoff > ready_at:
                ready_at = backoff
            soonest = min(soonest, max(0.0, (ready_at - moment).total_seconds()))
        return max(0.5, min(soonest, self.tick_interval))

    # ── one pass ──────────────────────────────────────────────────────────

    async def tick(self) -> TickReport:
        started = datetime.now(UTC)
        report = TickReport(started_at=utcnow())

        due = self.due_sources(started)
        report.due = len(due)
        if not due:
            return report

        by_company: dict[str, list[Source]] = {}
        for source in due:
            by_company.setdefault(source.company_slug, []).append(source)

        log.debug("tick_start", companies=len(by_company), sources=len(due))

        results = await asyncio.gather(
            *(self.pipeline.run_company(group) for group in by_company.values()),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, BaseException):
                # A company blowing up must not take the loop with it (§13.9).
                log.error("company_poll_crashed", error=str(result), exc_info=result)
                report.failed += 1
                continue
            report.reports.append(result)
            for outcome in result.outcomes:
                report.polled += 1
                if outcome.ok:
                    report.succeeded += 1
                elif outcome.outcome == "error":
                    report.failed += 1
                report.alerted += outcome.alerted

        report.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

        if report.polled:
            log.info(
                "tick_complete",
                due=report.due,
                polled=report.polled,
                ok=report.succeeded,
                failed=report.failed,
                alerted=report.alerted,
                ms=report.duration_ms,
            )

        if self.health is not None:
            # Heartbeat only when something actually worked: a process that is
            # alive but failing everything must not report healthy (§12).
            await self.health.after_tick(report.any_success)

        return report

    # ── the loop ──────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        self._running = True
        log.info(
            "scheduler_started",
            sources=len(self.all_sources()),
            seasonal_multiplier=self.settings.seasonal_multiplier(),
        )
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            # The loop is the product. Any tick failure is logged and the loop continues.
            except Exception as exc:
                log.error("tick_failed", error=str(exc), exc_info=True)

            # A little jitter so a restart storm does not resynchronize every
            # source onto the same instant.
            delay = self.seconds_until_next_due() + random.uniform(0, 0.5)
            await asyncio.sleep(delay)

    def stop(self) -> None:
        self._running = False

    # ── startup housekeeping ──────────────────────────────────────────────

    def startup_maintenance(self) -> None:
        pruned = self.db.prune_poll_log(self.settings.retention.poll_log_days)
        if pruned:
            log.info("poll_log_pruned", rows=pruned, days=self.settings.retention.poll_log_days)

        # A source stuck in a long backoff across a restart should get one prompt
        # retry: the restart is usually the fix.
        cleared = self.db.execute(
            "UPDATE sources SET backoff_until = NULL WHERE backoff_until > ?",
            (_iso_in_hours(1),),
        )
        if cleared.rowcount:
            log.info("long_backoffs_cleared", sources=cleared.rowcount)

        unseeded = self.db.scalar(
            "SELECT COUNT(*) FROM sources WHERE seeded = 0 AND enabled = 1", default=0
        )
        if unseeded:
            log.info(
                "sources_awaiting_seed",
                count=unseeded,
                note="their first poll classifies the whole board and sends one digest each "
                     "(classification.on_seed)",
            )


def _iso_in_hours(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
