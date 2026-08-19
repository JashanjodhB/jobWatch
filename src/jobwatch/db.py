"""SQLite schema, migrations, and connection management (§6).

One process, one connection, WAL. The dataset is small enough (tens of
thousands of rows at the outside) that every query here is sub-millisecond, so
the scheduler and the web UI share a single connection guarded by a reentrant
lock rather than paying for a pool or a thread executor.

The three tables that must never be pruned -- `jobs`, `alerted_merges`,
`title_verdicts` -- are why the system does not repeat itself (§13.4).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = ["SCHEMA_VERSION", "Database", "default_db_path", "parse_ts", "utcnow"]

SCHEMA_VERSION = 3


def utcnow() -> str:
    """ISO-8601 UTC, second precision, always suffixed 'Z'. The only clock we use."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def default_db_path() -> Path:
    """JOBWATCH_DB if set, else ./data/jobs.db.

    The spec's /var/lib/jobwatch/jobs.db is set via the environment file on the
    server; the relative default is what makes development on a laptop work.
    """
    env = os.environ.get("JOBWATCH_DB")
    if env:
        return Path(env).expanduser()
    return Path.cwd() / "data" / "jobs.db"


# ── schema ────────────────────────────────────────────────────────────────

_V1 = """
CREATE TABLE companies (
    slug              TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL,
    tier              TEXT NOT NULL CHECK(tier IN ('hot','warm','cold')),
    enabled           INTEGER NOT NULL DEFAULT 1,
    careers_url       TEXT,
    notes             TEXT
);

-- One row per (company, source). This is what the scheduler iterates.
CREATE TABLE sources (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    company_slug           TEXT NOT NULL REFERENCES companies(slug) ON DELETE CASCADE,
    adapter                TEXT NOT NULL,
    adapter_config         TEXT NOT NULL,        -- JSON
    priority               INTEGER NOT NULL,     -- 1 = canonical apply path
    enabled                INTEGER NOT NULL DEFAULT 1,
    fallback_adapter       TEXT,
    fallback_config        TEXT,
    -- scheduling state
    last_attempt_at        TEXT,
    last_success_at        TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    backoff_until          TEXT,
    using_fallback         INTEGER NOT NULL DEFAULT 0,
    -- conditional requests
    etag                   TEXT,
    last_modified          TEXT,
    -- drift detection
    baseline_posting_count INTEGER,
    seeded                 INTEGER NOT NULL DEFAULT 0,
    UNIQUE(company_slug, adapter)
);

CREATE INDEX idx_sources_due ON sources(enabled, backoff_until);

CREATE TABLE jobs (
    dedup_key         TEXT PRIMARY KEY,
    merge_key         TEXT NOT NULL,
    company_slug      TEXT NOT NULL REFERENCES companies(slug),
    source_id         INTEGER NOT NULL REFERENCES sources(id),
    req_id            TEXT,
    title             TEXT NOT NULL,
    normalized_title  TEXT NOT NULL,
    locations         TEXT NOT NULL,            -- JSON array
    url               TEXT NOT NULL,
    source_posted_at  TEXT,                     -- UNTRUSTED, display only
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    classification    TEXT NOT NULL DEFAULT 'pending'
                        CHECK(classification IN ('pending','match','reject','review')),
    class_source      TEXT,                     -- 'rules' | 'manual' | 'seed'
    category          TEXT,
    alerted_at        TEXT,
    app_status        TEXT DEFAULT 'none'
                        CHECK(app_status IN ('none','saved','applied','interview','offer','rejected')),
    app_updated_at    TEXT
);

CREATE INDEX idx_jobs_merge   ON jobs(merge_key);
CREATE INDEX idx_jobs_company ON jobs(company_slug);
CREATE INDEX idx_jobs_review  ON jobs(classification) WHERE classification = 'review';
CREATE INDEX idx_jobs_feed    ON jobs(first_seen_at DESC);

-- Records that a merge_key has been alerted. Separate table so it survives
-- any future change to how job rows are stored.
CREATE TABLE alerted_merges (
    merge_key    TEXT PRIMARY KEY,
    company_slug TEXT NOT NULL,
    alerted_at   TEXT NOT NULL,
    dedup_key    TEXT NOT NULL
);

-- Human verdicts, cached globally by normalized title.
-- This is what replaces the LLM classifier.
CREATE TABLE title_verdicts (
    normalized_title TEXT PRIMARY KEY,
    verdict          TEXT NOT NULL CHECK(verdict IN ('match','reject')),
    category         TEXT,
    source           TEXT NOT NULL CHECK(source IN ('rules','manual')),
    decided_at       TEXT NOT NULL,
    sample_title     TEXT,
    sample_company   TEXT
);

-- Editable classification rules. Seeded from filters.yaml, then UI-owned.
CREATE TABLE filter_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK(kind IN
                 ('require_any','role_any','exclude_any','location_exclude')),
    pattern    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, pattern)
);

CREATE TABLE outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    merge_key   TEXT NOT NULL,
    payload     TEXT NOT NULL,                  -- JSON
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    status      TEXT NOT NULL DEFAULT 'queued'
                  CHECK(status IN ('queued','sent','failed')),
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    -- delivery pacing; not part of the §6 minimum but needed for retry backoff
    next_attempt_at TEXT,
    kind        TEXT NOT NULL DEFAULT 'job'     -- 'job' | 'digest' | 'alarm'
);

CREATE INDEX idx_outbox_queued ON outbox(status) WHERE status = 'queued';

CREATE TABLE poll_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL,
    at            TEXT NOT NULL,
    outcome       TEXT NOT NULL,   -- 'ok'|'not_modified'|'error'|'empty'
    posting_count INTEGER,
    new_count     INTEGER,
    duration_ms   INTEGER,
    error         TEXT
);

CREATE INDEX idx_poll_log_at ON poll_log(at);

-- Rolling per-source posting-count history, for drift detection (§12).
CREATE TABLE source_baseline (
    source_id  INTEGER NOT NULL,
    day        TEXT NOT NULL,
    max_count  INTEGER NOT NULL,
    PRIMARY KEY (source_id, day)
);

-- Small durable key/value store: digest flush times, heartbeat state, alarms.
CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# v2: notifications fan out to more than one channel (Discord, email), so a row
# records which channels have already accepted it. Without this, a retry after a
# partial failure re-sends to the channel that already succeeded.
_V2 = """
ALTER TABLE outbox ADD COLUMN delivered TEXT;   -- JSON array of channel names
"""

# v3: 'location_require' turns location screening into an allow list, so a
# filter can say "United States only" instead of naming every country to skip.
# SQLite cannot widen a CHECK constraint in place, hence the table rebuild.
_V3 = """
CREATE TABLE filter_rules_v3 (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK(kind IN
                 ('require_any','role_any','exclude_any',
                  'location_exclude','location_require')),
    pattern    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, pattern)
);
INSERT INTO filter_rules_v3(id, kind, pattern, enabled, note, created_at)
    SELECT id, kind, pattern, enabled, note, created_at FROM filter_rules;
DROP TABLE filter_rules;
ALTER TABLE filter_rules_v3 RENAME TO filter_rules;
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1), (2, _V2), (3, _V3)]


# ── connection ────────────────────────────────────────────────────────────


class Database:
    """A single WAL connection, safe to share across the event loop and threads."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._depth = 0

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions only
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connect()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- migrations --------------------------------------------------------

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        with self._lock:
            conn = self.conn
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                # BEGIN/COMMIT live inside the script: executescript() commits any
                # open transaction before it runs, so wrapping it from out here
                # would leave nothing for COMMIT to close.
                conn.executescript(
                    f"BEGIN;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
                )
                current = version
            return current

    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    # -- access ------------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Reentrant write transaction. Only the outermost call commits."""
        with self._lock:
            conn = self.conn
            outermost = self._depth == 0
            if outermost:
                conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield conn
            except Exception:
                self._depth -= 1
                if outermost:
                    conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outermost:
                    conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, [tuple(p) for p in seq])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # -- small durable kv --------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- maintenance -------------------------------------------------------

    def prune_poll_log(self, days: int = 30) -> int:
        """Prune poll_log only. jobs / alerted_merges / title_verdicts are forever."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self.execute("DELETE FROM poll_log WHERE at < ?", (cutoff,))
        return cur.rowcount or 0

    def backup_to(self, destination: Path | str) -> Path:
        """Consistent online snapshot via the SQLite backup API."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(dest) as target:
            self.conn.backup(target)
        return dest
