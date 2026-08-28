"""Retroactive alerting for postings a cold start swallowed (§13.1).

Seeding is per *source*, so every bulk registry expansion buries the real
internships it discovers: the rows land as `class_source='seed'`, classified
`reject` without ever meeting the ruleset, with their `merge_key` suppressed in
`alerted_merges`. Nothing in the normal flow revisits them. `jobwatch replay`
cannot help either -- it re-queues outbox rows that already exist, and a seeded
job never got one.

This module re-runs the classifier over those rows, lifts the suppression
seeding wrote, and queues what the rules now consider alertable. It is a
separate command rather than something `run` does on startup because the blast
radius is thousands of messages: it wants `--dry-run` and scoping first.

**The re-run guard.** `alerted_merges` cannot distinguish a suppression row that
seeding wrote from one a genuine alert wrote -- both are just a merge_key and a
timestamp. `jobs.alerted_at` can: seeding never sets it and alerting always
does. So a merge_key is skipped whenever *any* job carrying it has `alerted_at`
set, which is what makes this command safe to run twice.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .classify.verdicts import Classifier
from .config import Settings
from .db import Database, utcnow
from .logging_setup import get_logger
from .notify.outbox import Outbox
from .pipeline import _json_list, build_job_payload

__all__ = ["BackfillReport", "BackfillScope", "backfill"]

log = get_logger(__name__)

# Kept distinct from 'rules' so a backfilled verdict stays auditable, and so the
# feed's backlog view empties out as rows are dealt with.
CLASS_SOURCE = "backfill"

# How far before a source's last posting-carrying poll a job may have last been
# seen and still count as live. Ingest and poll_log stamp their own clocks a beat
# apart, so an exact match would drop everything.
LIVE_SLACK_HOURS = 1.0


@dataclass(slots=True)
class BackfillScope:
    """What subset of the seed backlog to act on. Empty means everything."""

    tiers: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    match_only: bool = False
    since: str | None = None          # first_seen_at >= this ISO date
    include_stale: bool = False       # keep postings gone from the board
    limit: int | None = None          # cap on merge_keys queued, not rows scanned


@dataclass(slots=True)
class BackfillReport:
    dry_run: bool = False
    scanned: int = 0
    reclassified: int = 0
    verdicts: Counter[str] = field(default_factory=Counter)
    queued: int = 0
    skipped_already_alerted: int = 0
    skipped_duplicate_merge: int = 0
    skipped_on_review: int = 0
    truncated_by_limit: int = 0
    per_company: Counter[str] = field(default_factory=Counter)
    samples: list[tuple[str, str, str]] = field(default_factory=list)

    def __str__(self) -> str:
        verb = "would reclassify" if self.dry_run else "reclassified"
        queued = "would queue" if self.dry_run else "queued"
        companies = len(self.per_company)
        noun = "company" if companies == 1 else "companies"
        return (
            f"{verb} {self.reclassified} of {self.scanned} seeded postings "
            f"({self.verdicts.get('match', 0)} match, "
            f"{self.verdicts.get('review', 0)} review, "
            f"{self.verdicts.get('reject', 0)} reject) · "
            f"{queued} {self.queued} alert(s) across {companies} {noun}"
        )


def _select(scope: BackfillScope) -> tuple[str, list[Any]]:
    clauses = ["j.class_source = 'seed'"]
    args: list[Any] = []

    if scope.tiers:
        placeholders = ",".join("?" * len(scope.tiers))
        clauses.append(f"c.tier IN ({placeholders})")
        args.extend(scope.tiers)
    if scope.companies:
        placeholders = ",".join("?" * len(scope.companies))
        clauses.append(f"j.company_slug IN ({placeholders})")
        args.extend(scope.companies)
    if scope.since:
        clauses.append("j.first_seen_at >= ?")
        args.append(scope.since)
    if not scope.include_stale:
        # Still on the board as of the last poll that actually read one.
        #
        # NOT `sources.last_success_at`: a 304 counts as a successful poll and
        # bumps it, but `_ingest` never runs, so no job's `last_seen_at` moves.
        # Measuring against it therefore declares the entire board of every
        # correctly-caching source stale -- 29 of 44 hot sources here, holding
        # 3,903 seeded rows, OpenAI's 752 among them. `poll_log` is the honest
        # record of which polls carried postings.
        #
        # julianday, not datetime(): SQLite's datetime() returns a
        # space-separated string that compares wrong against the 'T'-and-'Z'
        # stamps stored here, silently widening the scope instead.
        clauses.append(
            "(pl.last_ingest_at IS NULL OR "
            "julianday(j.last_seen_at) >= julianday(pl.last_ingest_at) - ?)"
        )
        args.append(LIVE_SLACK_HOURS / 24.0)

    sql = f"""
        SELECT j.*, c.display_name, c.tier
        FROM jobs j
        JOIN companies c ON c.slug = j.company_slug
        JOIN sources s ON s.id = j.source_id
        LEFT JOIN (
            SELECT source_id, MAX(at) AS last_ingest_at
            FROM poll_log WHERE outcome IN ('ok', 'empty') GROUP BY source_id
        ) pl ON pl.source_id = s.id
        WHERE {" AND ".join(clauses)}
        ORDER BY c.tier, j.company_slug, j.first_seen_at, j.dedup_key
    """
    return sql, args


def backfill(
    db: Database,
    settings: Settings,
    outbox: Outbox,
    scope: BackfillScope | None = None,
    *,
    dry_run: bool = False,
) -> BackfillReport:
    """Reclassify the seed backlog in place and queue what should have alerted."""
    scope = scope or BackfillScope()
    report = BackfillReport(dry_run=dry_run)
    classifier = Classifier(db)
    on_review = settings.classification.on_review
    now = utcnow()

    sql, args = _select(scope)
    rows = db.query(sql, args)
    report.scanned = len(rows)
    if not rows:
        return report

    # A merge_key that ever produced a real alert is off limits; see the module
    # docstring. One query beats a per-row lookup over a six-figure backlog.
    truly_alerted = {
        r["merge_key"]
        for r in db.query("SELECT DISTINCT merge_key FROM jobs WHERE alerted_at IS NOT NULL")
    }

    queued_merges: set[str] = set()
    updates: list[tuple[str, str, str | None, str]] = []
    to_queue: list[tuple[Any, str, str | None]] = []

    for row in rows:
        decision = classifier.classify(
            row["title"], _json_list(row["locations"]), row["company_slug"]
        )
        report.verdicts[decision.classification] += 1
        updates.append(
            (decision.classification, CLASS_SOURCE, decision.category, row["dedup_key"])
        )

        if not decision.alertable:
            continue
        if scope.match_only and decision.classification != "match":
            continue
        if decision.classification == "review" and on_review == "queue_only":
            report.skipped_on_review += 1
            continue

        merge = row["merge_key"]
        if merge in truly_alerted:
            report.skipped_already_alerted += 1
            continue
        if merge in queued_merges:
            report.skipped_duplicate_merge += 1
            continue
        if scope.limit is not None and len(queued_merges) >= scope.limit:
            report.truncated_by_limit += 1
            continue

        queued_merges.add(merge)
        to_queue.append((row, decision.classification, decision.category))
        label = row["display_name"] or row["company_slug"]
        report.per_company[label] += 1
        if len(report.samples) < 20:
            report.samples.append((decision.classification, label, row["title"]))

    report.reclassified = len(updates)
    report.queued = len(to_queue)

    if dry_run:
        return report

    with db.tx() as conn:
        conn.executemany(
            "UPDATE jobs SET classification=?, class_source=?, category=? WHERE dedup_key=?",
            updates,
        )
        for row, classification, category in to_queue:
            merge = row["merge_key"]
            # Seeding's suppression row has to go before the real one lands: the
            # table is keyed by merge_key, so an INSERT alone would be ignored
            # and `alerted_at` would then disagree with `alerted_merges`.
            conn.execute("DELETE FROM alerted_merges WHERE merge_key = ?", (merge,))
            conn.execute(
                "INSERT INTO alerted_merges(merge_key, company_slug, alerted_at, dedup_key) "
                "VALUES(?,?,?,?)",
                (merge, row["company_slug"], now, row["dedup_key"]),
            )
            conn.execute(
                "UPDATE jobs SET alerted_at = ? WHERE dedup_key = ?",
                (now, row["dedup_key"]),
            )
            payload = build_job_payload(
                conn, row, classification, category, settings.web.base_url
            )
            payload["backfilled"] = True
            outbox.enqueue(merge, payload, kind="digest")

    log.info(
        "backfill_complete",
        scanned=report.scanned,
        reclassified=report.reclassified,
        queued=report.queued,
        companies=len(report.per_company),
    )
    return report
