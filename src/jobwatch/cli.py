"""Command line interface (§12).

The UI covers most of this. The CLI exists for headless debugging over SSH, and
for `--dry-run`, which is the tool you actually tune filters with: it runs the
whole pipeline against real boards and prints what would send, without sending
anything or marking anything alerted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from .config import AppConfig, export_config
from .db import Database
from .logging_setup import configure_logging, get_logger

log = get_logger("jobwatch.cli")

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _color(enabled: bool):
    if enabled:
        return DIM, BOLD, GREEN, RED, YELLOW, RESET
    return "", "", "", "", "", ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobwatch",
        description="Self-hosted internship posting monitor.",
    )
    parser.add_argument("--db", type=Path, help="database path (default: $JOBWATCH_DB or ./data/jobs.db)")
    parser.add_argument("--config-dir", type=Path, help="directory holding the YAML config")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json-logs", action="store_true", help="force JSON log output")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the scheduler and the web UI")
    run.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    run.add_argument("--no-web", action="store_true", help="scheduler only")
    run.add_argument("--once", action="store_true", help="one scheduler pass, then exit")

    notify = sub.add_parser(
        "notify-test", help="send a sample alert through every configured channel"
    )
    notify.add_argument(
        "--channel", help="only this channel (discord, email); default is all configured"
    )

    sub.add_parser("status", help="source health summary")
    sub.add_parser("migrate", help="create or upgrade the schema")

    seed = sub.add_parser("seed", help="import config/*.yaml into the database")
    seed.add_argument("--force", action="store_true", help="also overwrite existing rows from YAML")

    test = sub.add_parser("test", help="fetch one source once and print what it returned")
    test.add_argument("slug")
    test.add_argument("--adapter")
    test.add_argument("--raw", action="store_true", help="print full posting records as JSON")

    classify = sub.add_parser("classify", help="show how a title classifies, and why")
    classify.add_argument("title")
    classify.add_argument("--location", action="append", default=[])

    replay = sub.add_parser("replay", help="re-queue the alert for a merge_key")
    replay.add_argument("merge_key")

    disc = sub.add_parser("discover", help="identify the ATS behind a careers URL")
    disc.add_argument("careers_url")

    bulk = sub.add_parser(
        "discover-bulk",
        help="find companies you are not watching yet, from community internship feeds",
    )
    bulk.add_argument("--feed", action="append", default=[], metavar="URL",
                      help="feed JSON URL (repeatable); default is the built-in set")
    bulk.add_argument("--term", action="append", default=[], metavar="TERM",
                      help="only companies hiring for this term, e.g. 'Summer 2027' (repeatable)")
    bulk.add_argument("--category", action="append", default=[], metavar="CAT",
                      help="only this feed category, e.g. Software (repeatable)")
    bulk.add_argument("--out", type=Path, help="staging YAML to write (default config/companies-discovered.yaml)")
    bulk.add_argument("--ledger", type=Path, help="probe ledger JSON (default alongside the database)")
    bulk.add_argument("--limit", type=int, default=40,
                      help="max companies to probe this run (default 40)")
    bulk.add_argument("--min-listings", type=int, default=1,
                      help="skip companies with fewer active listings than this")
    bulk.add_argument("--tier", default="warm", choices=["hot", "warm", "cold"],
                      help="tier to write into the generated blocks")
    bulk.add_argument("--concurrency", type=int,
                      help="parallel probes (default: http.max_concurrency, halved)")
    bulk.add_argument("--retry-failed", action="store_true",
                      help="re-probe companies that failed in an earlier run")
    bulk.add_argument("--dry-run", action="store_true",
                      help="list what would be probed, without probing anything")

    sub.add_parser("export-config", help="write the database back out to YAML")
    sub.add_parser("adapters", help="list registered adapters")

    backup = sub.add_parser("backup", help="write a consistent snapshot of the database")
    backup.add_argument("destination", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level, force_json=True if args.json_logs else None)

    config = AppConfig.load(cfg_dir=args.config_dir, db_path=args.db)

    handlers = {
        "run": cmd_run,
        "notify-test": cmd_notify_test,
        "status": cmd_status,
        "migrate": cmd_migrate,
        "seed": cmd_seed,
        "test": cmd_test,
        "classify": cmd_classify,
        "replay": cmd_replay,
        "discover": cmd_discover,
        "discover-bulk": cmd_discover_bulk,
        "export-config": cmd_export,
        "adapters": cmd_adapters,
        "backup": cmd_backup,
    }
    try:
        return handlers[args.command](args, config)
    except KeyboardInterrupt:
        return 130


# ── commands ──────────────────────────────────────────────────────────────


def cmd_run(args, config: AppConfig) -> int:
    from .main import run_service

    return asyncio.run(
        run_service(
            config,
            dry_run=args.dry_run,
            with_web=not args.no_web,
            once=args.once,
        )
    )


def cmd_notify_test(args, config: AppConfig) -> int:
    return asyncio.run(_notify_test(args, config))


async def _notify_test(args, config: AppConfig) -> int:
    """Send one fabricated posting through each channel and report what happened.

    This is the setup check: it proves credentials, TLS, and formatting without
    waiting for a real posting to appear on a real board. It deliberately does
    not touch the database — nothing is queued, nothing is marked alerted.
    """
    from .db import utcnow
    from .http import build_client
    from .notify.base import DeliveryError
    from .service import build_channels

    dim, bold, green, red, yellow, reset = _color(sys.stdout.isatty())

    payload = {
        "type": "job",
        "company": "jobwatch",
        "company_slug": "jobwatch",
        "title": "Test alert — if you are reading this, delivery works",
        "url": "https://github.com/",
        "locations": ["Your machine"],
        "classification": "match",
        "category": "test",
        "detected_at": utcnow(),
        "merge_key": "test:notify",
        "ui_url": config.settings.web.base_url.rstrip("/") + "/outbox",
    }

    client = build_client(config.settings.http)
    channels = build_channels(config, client)
    if args.channel:
        channels = [c for c in channels if c.name == args.channel]
        if not channels:
            print(f"{red}no such channel: {args.channel}{reset}")
            await client.aclose()
            return 1

    print(f"\n{bold}notify-test{reset}")
    if config.email:
        print(f"  {dim}email → {config.email.summary}{reset}")

    failures = 0
    try:
        for channel in channels:
            if not channel.configured:
                print(f"  {yellow}-{reset} {channel.name:<10}not configured — skipped")
                continue
            try:
                await channel.send([payload], kind="job")
            except DeliveryError as exc:
                permanent = "permanent" if not exc.retryable else "retryable"
                print(f"  {red}x{reset} {channel.name:<10}{exc} {dim}({permanent}){reset}")
                failures += 1
            except Exception as exc:
                print(f"  {red}x{reset} {channel.name:<10}{type(exc).__name__}: {exc}")
                failures += 1
            else:
                print(f"  {green}+{reset} {channel.name:<10}delivered")
    finally:
        await client.aclose()

    if not any(c.configured for c in channels):
        print(
            f"\n  {yellow}Nothing is configured.{reset} Set DISCORD_WEBHOOK_URL, or "
            "SMTP_HOST and EMAIL_TO, in .env\n"
        )
        return 1
    print()
    return 1 if failures else 0


def cmd_migrate(_args, config: AppConfig) -> int:
    db = Database(config.db_path)
    version = db.migrate()
    db.close()
    print(f"schema at version {version} · {config.db_path}")
    return 0


def cmd_seed(args, config: AppConfig) -> int:
    from .config import load_companies, load_filters, seed_database

    db = Database(config.db_path)
    try:
        db.migrate()
        companies = load_companies(config.config_dir / "companies.yaml")
        filters = load_filters(config.config_dir / "filters.yaml")
        report = seed_database(db, companies, filters, force=args.force)
    finally:
        db.close()

    print(report)
    for warning in report.warnings:
        print(f"  warning: {warning}")
    if report.skipped_existing and not args.force:
        print(
            f"  {report.skipped_existing} existing companies left untouched "
            "(the UI owns them; use --force to overwrite from YAML)"
        )
    return 0


def cmd_status(_args, config: AppConfig) -> int:
    dim, bold, green, red, yellow, reset = _color(sys.stdout.isatty())
    db = Database(config.db_path)
    try:
        db.migrate()
        sources = db.query(
            """
            SELECT s.*, c.display_name, c.tier, c.enabled AS company_enabled
            FROM sources s JOIN companies c ON c.slug = s.company_slug
            ORDER BY s.consecutive_failures DESC, c.tier, s.company_slug
            """
        )
        jobs = db.scalar("SELECT COUNT(*) FROM jobs", default=0)
        alerted = db.scalar("SELECT COUNT(*) FROM alerted_merges", default=0)
        verdicts = db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0)
        review = db.scalar(
            "SELECT COUNT(DISTINCT normalized_title) FROM jobs WHERE classification = 'review' "
            "AND normalized_title NOT IN (SELECT normalized_title FROM title_verdicts)",
            default=0,
        )
        outbox = {
            r["status"]: r["n"]
            for r in db.query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        }

        print(f"\n{bold}jobwatch{reset}  {dim}{config.db_path}{reset}\n")
        print(
            f"  {jobs} postings tracked · {alerted} merge keys alerted · "
            f"{verdicts} title verdicts · {review} awaiting review"
        )
        print(
            f"  outbox: {outbox.get('queued', 0)} queued, {outbox.get('sent', 0)} sent, "
            f"{outbox.get('failed', 0)} failed\n"
        )

        if not sources:
            print(f"  {yellow}No sources configured. Run `jobwatch seed`.{reset}\n")
            return 0

        for source in sources:
            if not source["enabled"] or not source["company_enabled"]:
                mark, color, note = "-", dim, "disabled"
            elif source["consecutive_failures"] >= 5:
                mark, color = "x", red
                note = f"{source['consecutive_failures']} consecutive failures"
            elif source["consecutive_failures"]:
                mark, color = "!", yellow
                note = f"{source['consecutive_failures']} failures"
            elif not source["seeded"]:
                mark, color, note = "~", yellow, "awaiting first poll (will alert nothing)"
            else:
                mark, color = "+", green
                note = f"last ok {source['last_success_at'] or 'never'}"
            if source["using_fallback"]:
                note += f" · on {source['fallback_adapter']} fallback"

            print(
                f"  {color}{mark}{reset} {source['company_slug']:<22}"
                f"{dim}{source['adapter']:<18}{reset}{note}"
            )
        print()
    finally:
        db.close()
    return 0


def cmd_test(args, config: AppConfig) -> int:
    return asyncio.run(_test(args, config))


async def _test(args, config: AppConfig) -> int:
    from .adapters.base import AdapterError, FetchContext, get_adapter
    from .classify.verdicts import Classifier
    from .http import build_client
    from .normalize import normalize_title

    dim, bold, green, red, yellow, reset = _color(sys.stdout.isatty())
    db = Database(config.db_path)
    db.migrate()

    query = "SELECT * FROM sources WHERE company_slug = ?"
    params: list = [args.slug]
    if args.adapter:
        query += " AND adapter = ?"
        params.append(args.adapter)
    query += " ORDER BY priority"

    sources = db.query(query, params)
    if not sources:
        print(f"{red}no source configured for {args.slug!r}{reset}")
        db.close()
        return 1

    classifier = Classifier(db)
    client = build_client(config.settings.http)
    exit_code = 0

    try:
        for source in sources:
            adapter_name = source["adapter"]
            cfg = json.loads(source["adapter_config"] or "{}")
            print(f"\n{bold}{args.slug} · {adapter_name}{reset}  {dim}{cfg}{reset}")

            ctx = FetchContext(
                client=client, config=cfg, company_slug=args.slug,
                adapter_name=adapter_name, probe=True,
            )
            try:
                result = await get_adapter(adapter_name).fetch(ctx)
            except AdapterError as exc:
                print(f"  {red}FAIL{reset} {exc}")
                exit_code = 1
                continue
            except Exception as exc:
                print(f"  {red}FAIL{reset} {type(exc).__name__}: {exc}")
                exit_code = 1
                continue

            if result.not_modified:
                print(f"  {dim}304 Not Modified{reset}")
                continue

            print(f"  {green}{len(result.postings)} postings{reset}")
            if args.raw:
                for posting in result.postings:
                    print(json.dumps(posting.model_dump(), indent=2))
                continue

            for posting in result.postings[:25]:
                decision = classifier.explain(posting.title, posting.locations)
                tint = {"match": green, "review": yellow}.get(decision.classification, dim)
                places = ", ".join(posting.locations[:2])
                print(
                    f"    {tint}{decision.classification:<7}{reset}{posting.title[:66]:<68}"
                    f"{dim}{places[:28]}{reset}"
                )
                if decision.classification != "reject":
                    print(f"      {dim}→ {normalize_title(posting.title)}{reset}")
            if len(result.postings) > 25:
                print(f"    {dim}…and {len(result.postings) - 25} more{reset}")
    finally:
        await client.aclose()
        db.close()
    print()
    return exit_code


def cmd_classify(args, config: AppConfig) -> int:
    from .classify.verdicts import Classifier
    from .normalize import merge_key, normalize_title

    dim, bold, green, red, yellow, reset = _color(sys.stdout.isatty())
    db = Database(config.db_path)
    try:
        db.migrate()
        classifier = Classifier(db)
        decision = classifier.explain(args.title, args.location)
        tint = {"match": green, "review": yellow, "reject": red}.get(
            decision.classification, ""
        )

        print(f"\n  {bold}{args.title}{reset}")
        print(f"  {dim}normalized{reset}   {normalize_title(args.title)}")
        print(f"  {dim}merge key{reset}    {merge_key('<company>', args.title)}")
        print(f"  {dim}verdict{reset}      {tint}{decision.classification}{reset} "
              f"via {decision.class_source}")
        print(f"  {dim}because{reset}      {decision.reason}\n")
    finally:
        db.close()
    return 0


def cmd_replay(args, config: AppConfig) -> int:
    db = Database(config.db_path)
    try:
        db.migrate()
        cur = db.execute(
            "UPDATE outbox SET status='queued', attempts=0, last_error=NULL, "
            "next_attempt_at=NULL, sent_at=NULL WHERE merge_key = ?",
            (args.merge_key,),
        )
        print(f"re-queued {cur.rowcount} outbox row(s) for {args.merge_key}")
        if not cur.rowcount:
            print("  nothing matched — check `jobwatch status` or the outbox screen")
    finally:
        db.close()
    return 0


def cmd_discover(args, config: AppConfig) -> int:
    return asyncio.run(_discover(args, config))


async def _discover(args, config: AppConfig) -> int:
    from .discover import discover
    from .http import build_client

    dim, bold, green, _red, yellow, reset = _color(sys.stdout.isatty())
    client = build_client(config.settings.http)
    try:
        result = await discover(args.careers_url, client)
    finally:
        await client.aclose()

    print(f"\n{bold}{args.careers_url}{reset}")
    if result.page_error:
        print(f"  {dim}page fetch: {result.page_error}{reset}")

    for candidate in result.candidates:
        if candidate.verified:
            mark, color = "+", green
            note = f"verified · {candidate.posting_count} postings"
        else:
            mark, color = "?", yellow
            note = candidate.error or candidate.confidence
        print(f"\n  {color}{mark}{reset} {bold}{candidate.adapter}{reset}  {note}")
        print(f"    {dim}{candidate.evidence}{reset}")
        print(f"    config: {candidate.config}")
        if candidate.sample_title:
            print(f'    e.g. "{candidate.sample_title}"')

    best = result.best
    if best and best.verified:
        slug = args.careers_url.split("//")[-1].split("/")[0].split(".")[-2]
        print(f"\n{bold}Candidate registry block:{reset}\n")
        print(best.as_yaml_block(slug, slug.title(), args.careers_url))
    print()
    return 0 if (best and best.verified) else 1


def cmd_discover_bulk(args, config: AppConfig) -> int:
    return asyncio.run(_discover_bulk(args, config))


_SLUG_LINE = re.compile(r"^- slug:\s*(\S+)", re.M)


def _known_slugs(config: AppConfig, out_path: Path) -> set[str]:
    """Every company already accounted for: seeded, staged, or previously found.

    The staging files count. A company sitting in companies-expansion.yaml is
    one you have already decided about, and re-probing it every week would spend
    requests to re-learn something the repo already records.
    """
    known: set[str] = set()

    for name in ("companies.yaml", "companies-expansion.yaml"):
        path = config.config_dir / name
        if path.exists():
            known.update(_SLUG_LINE.findall(path.read_text(encoding="utf-8")))
    if out_path.exists():
        known.update(_SLUG_LINE.findall(out_path.read_text(encoding="utf-8")))

    # The database is authoritative once the UI has been used to add companies,
    # so a slug can be live without appearing in any YAML file.
    if config.db_path.exists():
        db = Database(config.db_path)
        try:
            known.update(r["slug"] for r in db.query("SELECT slug FROM companies"))
        # A missing or pre-migration database just means nothing extra to add.
        except Exception as exc:
            log.debug("could not read companies from the database: %s", exc)
        finally:
            db.close()
    return known


async def _discover_bulk(args, config: AppConfig) -> int:
    from .db import utcnow
    from .discover_bulk import (
        DEFAULT_FEEDS,
        Ledger,
        bulk_discover,
        collect_companies,
        fetch_feed,
        is_known,
        render_yaml,
    )
    from .http import build_client

    dim, bold, green, red, yellow, reset = _color(sys.stdout.isatty())

    out_path = args.out or (config.config_dir / "companies-discovered.yaml")
    ledger_path = args.ledger or (config.db_path.parent / "discovery-ledger.json")
    ledger = Ledger.load(ledger_path)
    known = _known_slugs(config, out_path)
    feeds = args.feed or list(DEFAULT_FEEDS)

    client = build_client(config.settings.http)
    listings = []
    try:
        for feed in feeds:
            try:
                got = await fetch_feed(feed, client)
            # A tracker that has moved or gone private must not stop the run.
            except Exception as exc:
                print(f"  {yellow}!{reset} {feed}\n    {dim}{type(exc).__name__}: {exc}{reset}")
                continue
            print(f"  {green}+{reset} {len(got)} listings from {dim}{feed.split('/')[4]}{reset}")
            listings.extend(got)

        if not listings:
            print(f"\n{red}no feed could be read{reset}\n")
            return 1

        companies = collect_companies(
            listings, terms=args.term, categories=args.category
        )

        skipped_known = skipped_ledger = skipped_small = 0
        queue = []
        for company in companies.values():
            if is_known(company.slug, known):
                skipped_known += 1
                continue
            if company.listings < args.min_listings:
                skipped_small += 1
                continue
            status = ledger.status(company.slug)
            if status == "verified" or (status == "failed" and not args.retry_failed):
                skipped_ledger += 1
                continue
            queue.append(company)

        # Cheap, high-confidence work first: a fingerprintable URL costs no page
        # fetch, so --limit buys the most coverage by spending it there.
        queue.sort(key=lambda c: (not c.fingerprintable, -c.listings, c.slug))
        total_new = len(queue)
        queue = queue[: max(0, args.limit)]

        print(
            f"\n{bold}{len(companies)}{reset} companies in feed  "
            f"{dim}·{reset}  {skipped_known} already known  "
            f"{dim}·{reset}  {skipped_ledger} probed before  "
            f"{dim}·{reset}  {skipped_small} below --min-listings"
        )
        print(f"{bold}{total_new}{reset} new to probe, taking {bold}{len(queue)}{reset} this run\n")

        if not queue:
            print(f"{dim}nothing to do{reset}\n")
            return 0

        if args.dry_run:
            for company in queue:
                mark = f"{green}url{reset}" if company.fingerprintable else f"{yellow}page{reset}"
                print(f"  [{mark}] {bold}{company.display_name}{reset} {dim}({company.listings}){reset}")
                print(f"        {dim}{company.careers_url or 'no usable URL'}{reset}")
            print(f"\n{dim}--dry-run: nothing was probed{reset}\n")
            return 0

        concurrency = args.concurrency or max(1, config.settings.http.max_concurrency // 2)
        outcomes = await bulk_discover(queue, client, concurrency=concurrency)
    finally:
        await client.aclose()

    verified = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]

    for outcome in sorted(verified, key=lambda o: -o.company.listings):
        candidate = outcome.candidate
        assert candidate is not None
        count = candidate.posting_count
        print(
            f"  {green}+{reset} {bold}{outcome.company.display_name}{reset}  "
            f"{candidate.adapter}  {dim}>={count} postings{reset}"
        )
        ledger.record(
            outcome.company.slug,
            "verified",
            adapter=candidate.adapter,
            careers_url=outcome.company.careers_url,
        )

    for outcome in failed:
        print(
            f"  {red}x{reset} {outcome.company.display_name}  "
            f"{dim}{(outcome.error or 'unknown')[:90]}{reset}"
        )
        ledger.record(outcome.company.slug, "failed", error=(outcome.error or "")[:200])

    ledger.save()

    if verified:
        rendered = render_yaml(outcomes, tier=args.tier)
        if out_path.exists():
            # Append, never overwrite: earlier runs found companies this one
            # deliberately skipped, and losing them would make the ledger lie.
            body = rendered.split("\n\n", 1)[1] if "\n\n" in rendered else rendered
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n# ── added {utcnow()[:10]} ──\n\n{body}")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")

    print(
        f"\n{bold}{len(verified)} verified{reset}, {len(failed)} not resolved  "
        f"{dim}·{reset}  ledger: {dim}{ledger_path}{reset}"
    )
    if verified:
        print(f"wrote {bold}{out_path}{reset}")
        print(f"{dim}review it, move what you want into companies.yaml, then `jobwatch seed`{reset}")
    print()
    return 0


def cmd_export(_args, config: AppConfig) -> int:
    db = Database(config.db_path)
    try:
        db.migrate()
        companies_yaml, filters_yaml = export_config(db)
    finally:
        db.close()

    companies_path = config.config_dir / "companies.export.yaml"
    filters_path = config.config_dir / "filters.export.yaml"
    companies_path.write_text(companies_yaml, encoding="utf-8")
    filters_path.write_text(filters_yaml, encoding="utf-8")
    print(f"wrote {companies_path}\nwrote {filters_path}")
    print("Review, then move over the originals and commit.")
    return 0


def cmd_adapters(_args, _config: AppConfig) -> int:
    from .adapters.base import known_adapters

    for name in known_adapters():
        print(name)
    return 0


def cmd_backup(args, config: AppConfig) -> int:
    db = Database(config.db_path)
    try:
        db.migrate()
        dest = db.backup_to(args.destination)
    finally:
        db.close()
    print(f"snapshot written to {dest} ({dest.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
