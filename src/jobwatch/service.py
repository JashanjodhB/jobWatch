"""Service assembly: one object holding every long-lived component.

Both the scheduler and the web UI run as asyncio tasks in this one process,
sharing the event loop and the SQLite connection manager, which is what keeps
writes serialized without lock contention (§10).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx

from .classify.verdicts import Classifier
from .config import AppConfig, load_companies, load_filters, seed_database
from .db import Database
from .health import HealthMonitor
from .http import build_client
from .logging_setup import get_logger
from .notify.base import Channel
from .notify.discord import DiscordChannel, DiscordSender
from .notify.email import EmailChannel, EmailSender
from .notify.outbox import Outbox
from .pipeline import Pipeline
from .scheduler import Scheduler

__all__ = ["Service", "build_channels", "build_service"]

log = get_logger(__name__)


def build_channels(config: AppConfig, client: httpx.AsyncClient) -> list[Channel]:
    """Every channel the configuration knows about, configured or not.

    Unconfigured channels are kept in the list on purpose: the outbox skips them
    at delivery time, and the UI needs them to say *which* one is missing its
    credentials rather than just reporting that something is.
    """
    return [
        DiscordChannel(
            DiscordSender(
                client=client,
                webhook_url=config.discord_webhook,
                max_per_second=config.settings.notifications.max_per_second,
            )
        ),
        EmailChannel(EmailSender(config.email)),
    ]


@dataclass
class Service:
    config: AppConfig
    db: Database
    client: httpx.AsyncClient
    classifier: Classifier
    outbox: Outbox
    pipeline: Pipeline
    scheduler: Scheduler
    health: HealthMonitor
    semaphore: asyncio.Semaphore

    async def aclose(self) -> None:
        await self.client.aclose()
        self.db.close()


def build_service(
    config: AppConfig,
    *,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
) -> Service:
    db = Database(config.db_path)
    db.migrate()

    http_client = client or build_client(config.settings.http)

    channels = build_channels(config, http_client)
    outbox = Outbox(
        db,
        channels,
        max_attempts=config.settings.notifications.max_attempts,
        digest_interval_minutes=config.settings.notifications.digest_interval_minutes,
        dry_run=dry_run,
    )
    classifier = Classifier(db)
    semaphore = asyncio.Semaphore(config.settings.http.max_concurrency)
    pipeline = Pipeline(
        db,
        classifier,
        outbox,
        config.settings,
        http_client,
        dry_run=dry_run,
        semaphore=semaphore,
    )
    health = HealthMonitor(
        db, http_client, outbox, healthcheck_url=config.healthcheck_url
    )
    scheduler = Scheduler(db, pipeline, config.settings, health=health)

    live = [channel.name for channel in outbox.live_channels]
    if not live and not dry_run:
        log.warning(
            "no_notification_channel",
            note=(
                "alerts will queue in the outbox and fail; set DISCORD_WEBHOOK_URL, "
                "or SMTP_HOST and EMAIL_TO, in .env"
            ),
        )
    else:
        log.info("notification_channels", channels=live, dry_run=dry_run)

    return Service(
        config=config,
        db=db,
        client=http_client,
        classifier=classifier,
        outbox=outbox,
        pipeline=pipeline,
        scheduler=scheduler,
        health=health,
        semaphore=semaphore,
    )


def bootstrap(config: AppConfig, *, force: bool = False) -> None:
    """Create the schema and import config/*.yaml. Safe to run repeatedly."""
    db = Database(config.db_path)
    try:
        db.migrate()
        companies = load_companies(config.config_dir / "companies.yaml")
        filters = load_filters(config.config_dir / "filters.yaml")
        report = seed_database(db, companies, filters, force=force)
        log.info(
            "seeded",
            companies_added=report.companies_added,
            sources_added=report.sources_added,
            rules_added=report.rules_added,
            skipped=report.skipped_existing,
        )
        for warning in report.warnings:
            log.warning("seed_warning", detail=warning)
    finally:
        db.close()


def ensure_ready(config: AppConfig) -> None:
    """Migrate, and seed only if the registry is empty. Used by `jobwatch run`."""
    db = Database(config.db_path)
    try:
        db.migrate()
        empty = db.scalar("SELECT COUNT(*) FROM companies", default=0) == 0
    finally:
        db.close()
    if empty:
        log.info("first_run_bootstrap", db=str(config.db_path))
        bootstrap(config)


def db_exists(path: Path) -> bool:
    return Path(path).exists()
