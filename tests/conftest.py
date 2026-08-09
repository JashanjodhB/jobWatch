"""Shared test fixtures. No network anywhere in this suite (§15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobwatch.adapters.base import FakeAdapter, register
from jobwatch.classify.verdicts import Classifier
from jobwatch.config import FilterSpec, Settings, load_filters, seed_database
from jobwatch.db import Database
from jobwatch.models import Source
from jobwatch.notify.discord import DiscordChannel
from jobwatch.notify.outbox import Outbox
from jobwatch.pipeline import Pipeline

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CONFIG_DIR = Path(__file__).parent.parent / "config"

# `sources` is UNIQUE(company_slug, adapter), so a company with several test
# sources needs a distinct adapter name for each. These aliases all behave
# exactly like `fake`.
FAKE_ALIASES = ["fake", "fake1", "fake2", "fake3"]
for _alias in FAKE_ALIASES[1:]:
    register(_alias)(type(f"Fake_{_alias}", (FakeAdapter,), {}))


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ── HTTP doubles ──────────────────────────────────────────────────────────


def json_client(payload: Any, *, status: int = 200, headers: dict | None = None) -> httpx.AsyncClient:
    """A client that answers every request with one canned JSON body."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def sequence_client(responses: list[Any]) -> httpx.AsyncClient:
    """Answers successive requests from a list. Used for pagination tests."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[index]
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.call_count = calls  # type: ignore[attr-defined]
    return client


def text_client(body: str, *, status: int = 200, content_type: str = "text/html") -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers={"content-type": content_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class RecordingSender:
    """Stands in for DiscordSender. Records embeds instead of posting them."""

    def __init__(self, *, fail_times: int = 0, fatal: bool = False) -> None:
        self.messages: list[list[dict]] = []
        self.fail_times = fail_times
        self.fatal = fatal
        self.attempts = 0
        self.configured = True

    async def send_embeds(self, embeds: list[dict], *, content: str | None = None) -> None:
        from jobwatch.notify.discord import DiscordError

        self.attempts += 1
        if self.fatal:
            raise DiscordError("webhook deleted", status=404, retryable=False)
        if self.attempts <= self.fail_times:
            raise DiscordError("transient network blip")
        self.messages.append(embeds)

    @property
    def sent_titles(self) -> list[str]:
        return [e.get("title", "") for batch in self.messages for e in batch]


# ── database and pipeline ─────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "tiers": {
                "hot": {"interval_seconds": 60, "batch_notifications": False},
                "warm": {"interval_seconds": 600, "batch_notifications": True},
                "cold": {"interval_seconds": 3600, "batch_notifications": True},
            },
            "seasonal_multipliers": {"default": 1.0},
            "http": {"timeout_seconds": 5, "max_concurrency": 4, "backoff_base_seconds": 30},
            "web": {"bind_host": "127.0.0.1", "bind_port": 8080},
        }
    )


@pytest.fixture
def filters() -> FilterSpec:
    return load_filters(CONFIG_DIR / "filters.yaml")


@pytest.fixture
def seeded_db(db: Database, filters: FilterSpec) -> Database:
    seed_database(db, [], filters)
    return db


def add_company(db: Database, slug: str, *, tier: str = "hot", name: str | None = None) -> None:
    db.execute(
        "INSERT OR IGNORE INTO companies(slug, display_name, tier, enabled) VALUES(?,?,?,1)",
        (slug, name or slug.title(), tier),
    )


def add_source(
    db: Database,
    slug: str,
    adapter: str = "fake",
    config: dict | None = None,
    *,
    priority: int = 1,
    seeded: bool = True,
    fallback_adapter: str | None = None,
    fallback_config: dict | None = None,
) -> int:
    cur = db.execute(
        "INSERT INTO sources(company_slug, adapter, adapter_config, priority, enabled, "
        "seeded, fallback_adapter, fallback_config) VALUES(?,?,?,?,1,?,?,?)",
        (
            slug,
            adapter,
            json.dumps(config or {}),
            priority,
            int(seeded),
            fallback_adapter,
            json.dumps(fallback_config or {}),
        ),
    )
    return int(cur.lastrowid)


def get_source(db: Database, source_id: int) -> Source:
    row = db.one(
        "SELECT s.*, c.tier AS tier, c.display_name AS display_name, "
        "c.enabled AS company_enabled FROM sources s "
        "JOIN companies c ON c.slug = s.company_slug WHERE s.id = ?",
        (source_id,),
    )
    assert row is not None
    return Source.from_row(row)


def set_postings(db: Database, source_id: int, postings: list[dict]) -> None:
    """Point a `fake` source at a new set of postings."""
    db.execute(
        "UPDATE sources SET adapter_config = ? WHERE id = ?",
        (json.dumps({"postings": postings}), source_id),
    )


def distinct_title(index: int, suffix: str = "Intern") -> str:
    """A title that survives normalization as a distinct merge key.

    Bare numbers cannot be used: `normalize_title` drops digit-only tokens on
    purpose, so "Intern 1" and "Intern 2" are the same job.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    tag = alphabet[index // 26 % 26] + alphabet[index % 26]
    return f"Software Engineer {suffix} Team{tag.upper()}"


def posting(title: str, req_id: str | None = None, **kw) -> dict:
    return {
        "title": title,
        "url": kw.pop("url", f"https://example.com/jobs/{req_id or title.replace(' ', '-')}"),
        "req_id": req_id,
        "locations": kw.pop("locations", ["Seattle, WA"]),
        **kw,
    }


@pytest.fixture
def make_pipeline(seeded_db: Database, settings: Settings):
    """Build a Pipeline wired to a recording sender. Returns (pipeline, sender)."""

    def factory(*, dry_run: bool = False, sender: RecordingSender | None = None):
        recorder = sender or RecordingSender()
        outbox = Outbox(seeded_db, [DiscordChannel(recorder)], dry_run=dry_run)  # type: ignore[list-item]
        classifier = Classifier(seeded_db)
        pipe = Pipeline(
            seeded_db,
            classifier,
            outbox,
            settings,
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
            dry_run=dry_run,
        )
        return pipe, recorder

    return factory
