"""The pipeline (§7). Order here is load-bearing.

    fetch → 304? stop → normalize → persist → collect ONLY genuinely new rows
          → seed silently, or classify → gate on merge_key → enqueue

Two invariants this module exists to hold:

**Never alert on cold start (§13.1).** The first poll of a source records every
posting as already-known and sends nothing. A first run firing three thousand
Discord messages rate-limits the webhook and destroys trust in the system on day
one. Seeding is per *source*, so adding a second source to an existing company,
or restoring a backup that is missing a source's rows, is silent too.

**Never alert twice for the same merge_key (§13.2).** With multiple sources per
company this is the default failure, not an edge case: the same job legitimately
arrives from the careers site and the ATS board with different requisition IDs.
`alerted_merges` is checked before every enqueue, and written in the same
transaction as the outbox row.

Sources belonging to one company are polled together so that cross-source
attribution is right: when two sources carry the same job, the alert links to
the lowest `priority` number, which is the company's own careers page.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .adapters.base import AdapterError, AdapterUnavailable, FetchContext, get_adapter
from .classify.verdicts import Classifier
from .config import Settings
from .db import Database, utcnow
from .http import backoff_delay
from .logging_setup import get_logger
from .models import FetchResult, Source
from .normalize import dedup_key, merge_key, normalize_locations, normalize_title
from .notify.outbox import Outbox

__all__ = ["CompanyReport", "Pipeline", "PollOutcome", "build_job_payload"]

log = get_logger(__name__)


@dataclass(slots=True)
class PollOutcome:
    """What one poll of one source did. Mirrors a `poll_log` row."""

    source: Source
    outcome: str                    # 'ok' | 'not_modified' | 'error' | 'empty' | 'skipped'
    posting_count: int = 0
    new_count: int = 0
    alerted: int = 0
    duration_ms: int = 0
    error: str | None = None
    result: FetchResult | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("ok", "not_modified", "empty")


@dataclass(slots=True)
class CompanyReport:
    company_slug: str
    outcomes: list[PollOutcome] = field(default_factory=list)

    @property
    def alerted(self) -> int:
        return sum(o.alerted for o in self.outcomes)

    @property
    def any_success(self) -> bool:
        return any(o.ok for o in self.outcomes)


class Pipeline:
    def __init__(
        self,
        db: Database,
        classifier: Classifier,
        outbox: Outbox,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        dry_run: bool = False,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.db = db
        self.classifier = classifier
        self.outbox = outbox
        self.settings = settings
        self.client = client
        self.dry_run = dry_run
        self.semaphore = semaphore or asyncio.Semaphore(settings.http.max_concurrency)

    # ── entry point ───────────────────────────────────────────────────────

    async def run_company(self, sources: list[Source]) -> CompanyReport:
        """Poll every due source of one company, then alert once per merge_key."""
        if not sources:
            return CompanyReport("")

        company_slug = sources[0].company_slug
        report = CompanyReport(company_slug)

        fetched = await asyncio.gather(
            *(self._fetch_one(src) for src in sources), return_exceptions=False
        )

        # Highest-priority source first: within a single cycle, whichever source
        # is processed first owns the merge_key, and priority 1 is the canonical
        # apply path (§5).
        fetched.sort(key=lambda pair: (pair[0].priority, pair[0].id))

        for source, outcome in fetched:
            self._apply_source_state(source, outcome)
            if outcome.outcome == "ok" and outcome.result is not None:
                try:
                    self._ingest(source, outcome)
                # §13.9: one source failing to ingest must not stop its siblings.
                except Exception as exc:
                    log.error(
                        "ingest_failed", source=source.label, error=str(exc), exc_info=True
                    )
                    outcome.outcome = "error"
                    outcome.error = f"ingest: {exc}"
            self._record_poll(source, outcome)
            report.outcomes.append(outcome)

        return report

    # ── fetch ─────────────────────────────────────────────────────────────

    async def _fetch_one(self, source: Source) -> tuple[Source, PollOutcome]:
        started = time.monotonic()
        adapter_name = source.active_adapter

        try:
            adapter = get_adapter(adapter_name)
        except AdapterError as exc:
            return source, PollOutcome(
                source, "error", error=str(exc), duration_ms=_ms(started)
            )

        ctx = FetchContext(
            client=self.client,
            config=source.active_config,
            company_slug=source.company_slug,
            adapter_name=adapter_name,
            etag=source.etag,
            last_modified=source.last_modified,
            source_id=source.id,
        )

        deadline = self.settings.http.fetch_deadline_seconds
        try:
            # Every fetch gets a deadline and a semaphore slot. One hanging
            # request must never delay the others (§13.9). The deadline covers
            # the whole fetch including pagination; httpx enforces the much
            # tighter per-request timeout underneath it.
            async with self.semaphore:
                result = await asyncio.wait_for(adapter.fetch(ctx), timeout=deadline)
        except AdapterUnavailable as exc:
            # A missing optional extra is not a source failure; it degrades one
            # adapter and says so once per poll (§3).
            log.info("source_skipped_missing_extra", source=source.label, reason=str(exc))
            return source, PollOutcome(
                source, "skipped", error=str(exc), duration_ms=_ms(started)
            )
        except TimeoutError:
            return source, PollOutcome(
                source,
                "error",
                error=f"fetch exceeded the {deadline:.0f}s deadline",
                duration_ms=_ms(started),
            )
        except AdapterError as exc:
            return source, PollOutcome(source, "error", error=str(exc), duration_ms=_ms(started))
        except asyncio.CancelledError:
            raise
        # Adapters touch the open internet; the surface of what they can raise
        # is wide, and none of it is worth stopping the tick for.
        except Exception as exc:
            return source, PollOutcome(
                source,
                "error",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=_ms(started),
            )

        if result.not_modified:
            return source, PollOutcome(
                source, "not_modified", duration_ms=_ms(started), result=result
            )

        outcome = "ok" if result.postings else "empty"
        return source, PollOutcome(
            source,
            outcome,
            posting_count=len(result.postings),
            duration_ms=_ms(started),
            result=result,
        )

    # ── persist, classify, enqueue ────────────────────────────────────────

    def _ingest(self, source: Source, outcome: PollOutcome) -> None:
        result = outcome.result
        assert result is not None
        now = utcnow()

        with self.db.tx() as conn:
            new_keys = self._persist(conn, source, result, now)
            outcome.new_count = len(new_keys)

            if not source.seeded:
                outcome.alerted = self._seed(conn, source, new_keys, now)
                log.info(
                    "source_seeded",
                    source=source.label,
                    postings=len(result.postings),
                    recorded=len(new_keys),
                    alerted=outcome.alerted,
                    mode=self.settings.classification.on_seed,
                )
                return

            outcome.alerted = self._classify_and_alert(conn, source, new_keys, now)

    def _persist(
        self, conn: Any, source: Source, result: FetchResult, now: str
    ) -> list[str]:
        """Upsert every posting; return only the dedup_keys this call created.

        `dedup_key` is scoped by the *configured* adapter name, not the active
        one, so flipping a source to its fallback does not duplicate every row.
        """
        new_keys: list[str] = []
        seen_this_batch: set[str] = set()

        for posting in result.postings:
            dk = dedup_key(source.company_slug, source.adapter, posting.req_id, posting.title)
            if dk in seen_this_batch:
                continue
            seen_this_batch.add(dk)

            mk = merge_key(source.company_slug, posting.title)
            normalized = normalize_title(posting.title)
            locations = json.dumps(normalize_locations(posting.locations))

            exists = conn.execute(
                "SELECT 1 FROM jobs WHERE dedup_key = ?", (dk,)
            ).fetchone()

            if exists:
                # Postings vanish and reappear from pagination glitches and
                # recruiter edits; refresh, never delete (§13.4).
                conn.execute(
                    "UPDATE jobs SET last_seen_at=?, locations=?, url=?, title=?, "
                    "normalized_title=?, source_posted_at=? WHERE dedup_key=?",
                    (now, locations, posting.url, posting.title, normalized,
                     posting.posted_at, dk),
                )
                continue

            conn.execute(
                """
                INSERT INTO jobs(
                    dedup_key, merge_key, company_slug, source_id, req_id, title,
                    normalized_title, locations, url, source_posted_at,
                    first_seen_at, last_seen_at, classification)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')
                """,
                (
                    dk, mk, source.company_slug, source.id, posting.req_id, posting.title,
                    normalized, locations, posting.url, posting.posted_at, now, now,
                ),
            )
            new_keys.append(dk)

        return new_keys

    def _seed(self, conn: Any, source: Source, new_keys: list[str], now: str) -> int:
        """First poll of this source. Returns how many alerts it queued (§13.1).

        What cold start must never do is *flood*: a bulk registry expansion seeds
        hundreds of sources at once, and alerting each one's whole board
        individually rate-limits the webhook on day one. It does not follow that
        it must be *silent* — silence buries every real posting those sources
        discover, and nothing in the normal flow ever revisits a seeded row
        (`replay` only re-queues outbox rows, which a seeded job never got).

        `on_seed: digest` is the middle ground and the default: classify the
        board normally, then batch the alertable postings into one message, which
        is bounded at 1 no matter how large the board is. Rejects are suppressed
        exactly as `silent` suppressed everything.
        """
        mode = self.settings.classification.on_seed
        alerted = 0

        if mode == "silent":
            for dk in new_keys:
                conn.execute(
                    "UPDATE jobs SET classification='reject', class_source='seed' "
                    "WHERE dedup_key=?",
                    (dk,),
                )
        else:
            alerted = self._classify_and_alert(
                conn, source, new_keys, now, kind="job" if mode == "alert" else "digest"
            )

        # Every merge_key this source just saw is suppressed, alerted or not, so
        # the same job surfacing later from a *different* source does not alert
        # again. `OR IGNORE` leaves the rows `_classify_and_alert` already wrote.
        for dk in new_keys:
            row = conn.execute(
                "SELECT merge_key FROM jobs WHERE dedup_key = ?", (dk,)
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO alerted_merges(merge_key, company_slug, alerted_at, dedup_key) "
                    "VALUES(?,?,?,?)",
                    (row["merge_key"], source.company_slug, now, dk),
                )

        conn.execute("UPDATE sources SET seeded = 1 WHERE id = ?", (source.id,))
        source.seeded = True
        return alerted

    def _classify_and_alert(
        self,
        conn: Any,
        source: Source,
        new_keys: list[str],
        now: str,
        *,
        kind: str | None = None,
    ) -> int:
        """`kind` overrides the per-tier immediate/digest choice, which is how a
        cold start forces its whole board into one batched message."""
        alerted = 0
        on_review = self.settings.classification.on_review

        for dk in new_keys:
            row = conn.execute(
                "SELECT j.*, c.display_name, c.tier FROM jobs j "
                "JOIN companies c ON c.slug = j.company_slug WHERE j.dedup_key = ?",
                (dk,),
            ).fetchone()
            if row is None:
                continue

            locations = _json_list(row["locations"])
            decision = self.classifier.classify(
                row["title"], locations, source.company_slug
            )

            conn.execute(
                "UPDATE jobs SET classification=?, class_source=?, category=? WHERE dedup_key=?",
                (decision.classification, decision.class_source, decision.category, dk),
            )

            if not decision.alertable:
                continue
            if decision.classification == "review" and on_review == "queue_only":
                continue

            mk = row["merge_key"]
            already = conn.execute(
                "SELECT 1 FROM alerted_merges WHERE merge_key = ?", (mk,)
            ).fetchone()
            if already:
                # Same job, second source. The row is persisted and its URL is
                # available in the UI; sending again would be the duplicate the
                # whole design exists to prevent (§13.2).
                log.info(
                    "alert_suppressed_duplicate_merge",
                    source=source.label,
                    title=row["title"],
                    merge_key=mk,
                )
                continue

            # Written in the same transaction as the outbox row: a crash cannot
            # leave a merge marked alerted with nothing queued, or the reverse.
            conn.execute(
                "INSERT INTO alerted_merges(merge_key, company_slug, alerted_at, dedup_key) "
                "VALUES(?,?,?,?)",
                (mk, source.company_slug, now, dk),
            )
            conn.execute("UPDATE jobs SET alerted_at = ? WHERE dedup_key = ?", (now, dk))

            payload = self._payload(conn, row, decision.classification, decision.category)
            row_kind = kind or ("digest" if self.settings.batches(row["tier"]) else "job")
            self.outbox.enqueue(mk, payload, kind=row_kind)
            alerted += 1

        return alerted

    def _payload(
        self, conn: Any, row: Any, classification: str, category: str | None
    ) -> dict[str, Any]:
        return build_job_payload(
            conn, row, classification, category, self.settings.web.base_url
        )

    # ── source state ──────────────────────────────────────────────────────

    def _apply_source_state(self, source: Source, outcome: PollOutcome) -> None:
        """Update backoff, failure count, fallback flip, and conditional headers."""
        now = utcnow()
        http = self.settings.http

        if outcome.outcome == "skipped":
            self.db.execute("UPDATE sources SET last_attempt_at=? WHERE id=?", (now, source.id))
            return

        if outcome.ok:
            etag = outcome.result.etag if outcome.result else None
            last_modified = outcome.result.last_modified if outcome.result else None
            flipped_back = source.using_fallback and source.fallback_adapter is not None

            self.db.execute(
                "UPDATE sources SET last_attempt_at=?, last_success_at=?, "
                "consecutive_failures=0, backoff_until=NULL, using_fallback=0, "
                "etag=COALESCE(?, etag), last_modified=COALESCE(?, last_modified) "
                "WHERE id=?",
                (now, now, etag, last_modified, source.id),
            )
            if flipped_back:
                log.info("source_primary_recovered", source=source.label)
            source.consecutive_failures = 0
            source.using_fallback = False
            return

        failures = source.consecutive_failures + 1
        delay = backoff_delay(
            failures, http.backoff_base_seconds, http.backoff_max_seconds
        )
        backoff_until = _iso_in(delay)

        use_fallback = source.using_fallback
        if (
            not use_fallback
            and failures >= http.fallback_after_failures
            and source.fallback_adapter
        ):
            use_fallback = True
            log.warning(
                "source_flipped_to_fallback",
                source=source.label,
                fallback=source.fallback_adapter,
                failures=failures,
            )

        self.db.execute(
            "UPDATE sources SET last_attempt_at=?, consecutive_failures=?, "
            "backoff_until=?, using_fallback=? WHERE id=?",
            (now, failures, backoff_until, int(use_fallback), source.id),
        )
        source.consecutive_failures = failures
        source.using_fallback = use_fallback
        source.backoff_until = backoff_until

        log.warning(
            "source_poll_failed",
            source=source.label,
            adapter=source.active_adapter,
            failures=failures,
            retry_in_s=round(delay),
            error=(outcome.error or "")[:300],
        )

    def _record_poll(self, source: Source, outcome: PollOutcome) -> None:
        self.db.execute(
            "INSERT INTO poll_log(source_id, at, outcome, posting_count, new_count, "
            "duration_ms, error) VALUES(?,?,?,?,?,?,?)",
            (
                source.id,
                utcnow(),
                outcome.outcome,
                outcome.posting_count,
                outcome.new_count,
                outcome.duration_ms,
                (outcome.error or None) and outcome.error[:500],
            ),
        )

        # Rolling daily maximum, which drift detection compares against (§12).
        if outcome.outcome in ("ok", "empty"):
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            self.db.execute(
                "INSERT INTO source_baseline(source_id, day, max_count) VALUES(?,?,?) "
                "ON CONFLICT(source_id, day) DO UPDATE SET "
                "max_count = MAX(source_baseline.max_count, excluded.max_count)",
                (source.id, day, outcome.posting_count),
            )


def build_job_payload(
    conn: Any,
    row: Any,
    classification: str,
    category: str | None,
    base_url: str,
) -> dict[str, Any]:
    """Build the Discord payload, attributing the link to the canonical source.

    Module-level because `backfill.py` builds the same payload for postings a
    cold start swallowed; the shape has to stay identical or a backfilled alert
    renders differently from a live one.
    """
    best = conn.execute(
        "SELECT j.url, j.locations FROM jobs j JOIN sources s ON s.id = j.source_id "
        "WHERE j.merge_key = ? ORDER BY s.priority ASC, s.id ASC LIMIT 1",
        (row["merge_key"],),
    ).fetchone()

    url = best["url"] if best else row["url"]
    locations = _json_list(best["locations"]) if best else _json_list(row["locations"])
    if not locations:
        locations = _json_list(row["locations"])

    base = base_url.rstrip("/")
    return {
        "type": "job",
        "company": row["display_name"],
        "company_slug": row["company_slug"],
        "title": row["title"],
        "url": url,
        "locations": locations,
        "classification": classification,
        "category": category,
        "detected_at": row["first_seen_at"],
        "tier": row["tier"],
        "merge_key": row["merge_key"],
        "dedup_key": row["dedup_key"],
        "ui_url": f"{base}/?q={row['merge_key'][:12]}",
    }


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _iso_in(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []
