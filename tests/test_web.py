"""Web UI: every screen renders, and no route can take the poller down (§10, §13.8)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import (
    RecordingSender,
    add_company,
    add_source,
    distinct_title,
    get_source,
    posting,
    set_postings,
)
from jobwatch.classify.verdicts import Classifier
from jobwatch.config import AppConfig
from jobwatch.db import utcnow
from jobwatch.normalize import dedup_key, merge_key, normalize_title
from jobwatch.notify.discord import DiscordChannel
from jobwatch.notify.email import EmailChannel, EmailSender
from jobwatch.notify.outbox import Outbox
from jobwatch.pipeline import Pipeline
from jobwatch.scheduler import Scheduler
from jobwatch.service import Service
from jobwatch.web.app import create_app


@pytest.fixture
def service(seeded_db, settings, tmp_path) -> Service:
    sender = RecordingSender()
    outbox = Outbox(seeded_db, [DiscordChannel(sender)])  # type: ignore[list-item]
    classifier = Classifier(seeded_db)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"jobs": []}))
    )
    pipeline = Pipeline(seeded_db, classifier, outbox, settings, client)
    health = None  # not needed for route tests
    return Service(
        config=AppConfig(settings=settings, db_path=tmp_path / "x.db", config_dir=tmp_path),
        db=seeded_db,
        client=client,
        classifier=classifier,
        outbox=outbox,
        pipeline=pipeline,
        scheduler=Scheduler(seeded_db, pipeline, settings),
        health=health,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(4),
    )


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service), raise_server_exceptions=False)


def insert_job(db, *, title: str, company: str = "stripe", source_id: int = 1,
               classification: str = "match", req: str | None = None, seen: str | None = None):
    key = dedup_key(company, "fake", req, title)
    db.execute(
        "INSERT OR REPLACE INTO jobs(dedup_key, merge_key, company_slug, source_id, req_id, "
        "title, normalized_title, locations, url, first_seen_at, last_seen_at, "
        "classification, class_source, alerted_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'rules',?)",
        (key, merge_key(company, title), company, source_id, req, title,
         normalize_title(title), json.dumps(["Austin, TX"]),
         f"https://example.com/{key}", seen or utcnow(), seen or utcnow(),
         classification, utcnow()),
    )
    return key


@pytest.fixture
def populated(seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    insert_job(seeded_db, title="Software Engineer Intern", source_id=sid, req="1")
    insert_job(seeded_db, title="Sustainability Intern", source_id=sid, req="2",
               classification="review")
    return sid


# ── every screen renders ──────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/review", "/filters", "/companies", "/health", "/outbox"])
def test_screen_renders(client, populated, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:400]
    assert "<html" in response.text
    assert "jobwatch" in response.text


def test_healthz_is_plain_and_cheap(client):
    assert client.get("/healthz").text == "ok"


def test_feed_shows_matched_postings_with_an_elapsed_time_column(client, populated):
    body = client.get("/").text
    assert "Software Engineer Intern" in body
    assert 'class="age mono"' in body
    assert "data-ts=" in body, "the live ticker has nothing to update"


def test_feed_empty_state_is_an_invitation_not_a_shrug(client, seeded_db):
    body = client.get("/").text
    assert "No arrivals yet" in body
    assert "No data" not in body


def test_review_empty_state_wording(client, seeded_db):
    assert "rules are handling everything" in client.get("/review").text


def test_feed_collapses_a_job_carried_by_two_sources_into_one_row(client, seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    a = add_source(seeded_db, "stripe", "fake", priority=1)
    b = add_source(seeded_db, "stripe", "fake1", priority=2)
    # Same merge key, different rows.
    insert_job(seeded_db, title="Software Engineer Intern", source_id=a, req="A")
    insert_job(seeded_db, title="Software Engineering Internship", source_id=b, req="B")

    body = client.get("/").text
    assert body.count('class="row-title"') == 1


def test_feed_filters_narrow_the_result_set(client, populated):
    assert "Sustainability Intern" in client.get("/?show=review").text
    assert "Software Engineer Intern" not in client.get("/?show=review").text
    assert "Software Engineer Intern" in client.get("/?q=Software").text
    assert client.get("/?company=nope").status_code == 200


def test_feed_renders_500_jobs(client, seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    for i in range(500):
        insert_job(seeded_db, title=distinct_title(i), source_id=sid, req=str(i))

    response = client.get("/")
    assert response.status_code == 200
    assert response.text.count('class="row-title"') == 100  # first page


# ── application status ────────────────────────────────────────────────────


def test_marking_a_job_applied_is_one_request(client, seeded_db, populated):
    key = seeded_db.scalar("SELECT dedup_key FROM jobs WHERE req_id = '1'")
    response = client.post(f"/jobs/{key}/status", data={"status": "applied"})

    assert response.status_code == 200
    assert seeded_db.scalar("SELECT app_status FROM jobs WHERE dedup_key = ?", (key,)) == "applied"


def test_status_applies_to_every_row_of_the_same_job(client, seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    a = add_source(seeded_db, "stripe", "fake", priority=1)
    b = add_source(seeded_db, "stripe", "fake1", priority=2)
    key = insert_job(seeded_db, title="Software Engineer Intern", source_id=a, req="A")
    insert_job(seeded_db, title="Software Engineering Internship", source_id=b, req="B")

    client.post(f"/jobs/{key}/status", data={"status": "applied"})

    statuses = {r["app_status"] for r in seeded_db.query("SELECT app_status FROM jobs")}
    assert statuses == {"applied"}


def test_an_invalid_status_falls_back_to_none(client, seeded_db, populated):
    key = seeded_db.scalar("SELECT dedup_key FROM jobs WHERE req_id = '1'")
    client.post(f"/jobs/{key}/status", data={"status": "'; DROP TABLE jobs;--"})
    assert seeded_db.scalar("SELECT app_status FROM jobs WHERE dedup_key = ?", (key,)) == "none"
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == 2


# ── review queue ──────────────────────────────────────────────────────────


def test_review_card_explains_why_the_item_is_ambiguous(client, populated):
    body = client.get("/review").text
    assert "Sustainability Intern" in body
    assert "Why it is here" in body
    assert "no role_any" in body


def test_review_screen_documents_its_keyboard_bindings(client, populated):
    body = client.get("/review").text
    for key in ("<kbd>M</kbd>", "<kbd>R</kbd>", "<kbd>J</kbd>", "<kbd>K</kbd>", "<kbd>U</kbd>"):
        assert key in body


def test_a_verdict_is_recorded_and_the_next_card_returned(client, seeded_db, populated):
    key = seeded_db.scalar("SELECT dedup_key FROM jobs WHERE req_id = '2'")

    response = client.post(f"/review/{key}/verdict", data={"verdict": "match", "category": "infra"})

    assert response.status_code == 200
    row = seeded_db.one(
        "SELECT verdict, source, category FROM title_verdicts WHERE normalized_title = ?",
        (normalize_title("Sustainability Intern"),),
    )
    assert (row["verdict"], row["source"], row["category"]) == ("match", "manual", "infra")
    assert seeded_db.scalar("SELECT classification FROM jobs WHERE req_id='2'") == "match"
    assert "rules are handling everything" in response.text, "queue should now be empty"


def test_undo_puts_the_item_back_in_the_queue(client, seeded_db, populated):
    key = seeded_db.scalar("SELECT dedup_key FROM jobs WHERE req_id = '2'")
    client.post(f"/review/{key}/verdict", data={"verdict": "reject"})

    response = client.post(
        "/review/undo", data={"normalized_title": normalize_title("Sustainability Intern")}
    )

    assert response.status_code == 200
    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 0
    assert seeded_db.scalar("SELECT classification FROM jobs WHERE req_id='2'") == "review"


def test_thirty_items_can_be_cleared_in_thirty_requests(client, seeded_db):
    """Phase 6 acceptance."""
    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    for i in range(30):
        insert_job(seeded_db, title=distinct_title(i, "Sustainability Intern"),
                   source_id=sid, req=str(i), classification="review")

    for _ in range(30):
        card = client.get("/review/card?offset=0").text
        if "rules are handling everything" in card:
            break
        key = card.split('hx-post="/review/')[1].split("/verdict")[0]
        client.post(f"/review/{key}/verdict", data={"verdict": "reject"})

    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 30
    assert "rules are handling everything" in client.get("/review").text


def test_a_bogus_verdict_key_does_not_500(client, populated):
    assert client.post("/review/nosuchkey/verdict", data={"verdict": "match"}).status_code == 200


# ── filter workbench ──────────────────────────────────────────────────────


def test_preview_shows_a_diff_in_both_directions(client, seeded_db, populated):
    response = client.post(
        "/filters/preview", data={"kind": "role_any", "pattern": r"\bsustainability\b", "action": "add"}
    )
    assert response.status_code == 200
    assert "become matched" in response.text
    assert "Sustainability Intern" in response.text


def test_preview_reports_an_invalid_regex_without_saving_it(client, seeded_db):
    response = client.post("/filters/preview", data={"kind": "role_any", "pattern": "([unclosed"})
    assert "invalid regex" in response.text

    client.post("/filters", data={"kind": "role_any", "pattern": "([unclosed"})
    assert seeded_db.scalar(
        "SELECT COUNT(*) FROM filter_rules WHERE pattern = '([unclosed'", default=0
    ) == 0


def test_saving_a_rule_purges_cached_rule_verdicts(client, seeded_db, populated):
    Classifier(seeded_db).classify("Software Engineer Intern")
    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 1

    client.post("/filters", data={"kind": "role_any", "pattern": r"\bsustainability\b"})

    assert seeded_db.scalar("SELECT COUNT(*) FROM title_verdicts", default=0) == 0
    assert seeded_db.scalar(
        "SELECT COUNT(*) FROM filter_rules WHERE pattern = '\\bsustainability\\b'", default=0
    ) == 1


def test_toggling_and_deleting_a_rule(client, seeded_db):
    rule_id = seeded_db.scalar("SELECT id FROM filter_rules LIMIT 1")

    client.post(f"/filters/{rule_id}/toggle")
    assert seeded_db.scalar("SELECT enabled FROM filter_rules WHERE id = ?", (rule_id,)) == 0

    client.post(f"/filters/{rule_id}/delete")
    assert seeded_db.scalar("SELECT COUNT(*) FROM filter_rules WHERE id = ?", (rule_id,), default=0) == 0


def test_filters_export_downloads_valid_yaml(client, seeded_db):
    import yaml

    response = client.get("/filters/export")
    assert response.status_code == 200
    assert "filters.yaml" in response.headers["content-disposition"]
    parsed = yaml.safe_load(response.text)
    assert "require_any" in parsed


# ── companies ─────────────────────────────────────────────────────────────


def test_test_fetch_shows_parsed_postings_and_persists_nothing(client, seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting("Software Engineer Intern", "1")])
    before = seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0)

    response = client.post("/companies/stripe/test")

    assert response.status_code == 200
    assert "Software Engineer Intern" in response.text
    assert "1 postings" in response.text or "returned" in response.text
    assert seeded_db.scalar("SELECT COUNT(*) FROM jobs", default=0) == before


def test_test_fetch_reports_an_adapter_failure_readably(client, seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    add_source(seeded_db, "stripe", "fake", {"raise": "board is gone"})

    response = client.post("/companies/stripe/test")

    assert response.status_code == 200
    assert "board is gone" in response.text


def test_toggling_a_source_disables_rather_than_deletes(client, seeded_db):
    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe")

    client.post(f"/sources/{sid}/toggle")

    assert seeded_db.scalar("SELECT enabled FROM sources WHERE id = ?", (sid,)) == 0
    assert seeded_db.scalar("SELECT COUNT(*) FROM sources", default=0) == 1


# ── outbox ────────────────────────────────────────────────────────────────


def test_outbox_lists_rows_and_replays_them(client, seeded_db, service):
    service.outbox.enqueue("mk-1", {"title": "Software Engineer Intern", "company": "Stripe"})
    seeded_db.execute("UPDATE outbox SET status = 'failed', last_error = 'boom'")

    body = client.get("/outbox?status=failed").text
    assert "Software Engineer Intern" in body
    assert "boom" in body

    client.post("/outbox/1/replay")
    assert seeded_db.scalar("SELECT status FROM outbox WHERE id = 1") == "queued"


def test_outbox_warns_when_no_channel_is_configured(client, service):
    unconfigured = RecordingSender()
    unconfigured.configured = False
    service.outbox.channels = [DiscordChannel(unconfigured)]  # type: ignore[list-item]

    body = client.get("/outbox").text

    assert "No notification channel configured" in body
    assert "SMTP_HOST" in body, "the banner should name the email route too"


def test_outbox_lists_which_channels_are_live(client, service):
    configured, missing = RecordingSender(), RecordingSender()
    missing.configured = False
    service.outbox.channels = [  # type: ignore[list-item]
        DiscordChannel(configured),
        EmailChannel(EmailSender(None)),
    ]

    body = client.get("/outbox").text

    assert "No notification channel configured" not in body, "discord is live"
    assert "discord" in body and "email" in body


def test_outbox_shows_which_channels_already_took_a_partially_delivered_row(
    client, service, seeded_db
):
    """A queued row that Discord already accepted must not read as undelivered."""
    service.outbox.enqueue("mk-partial", {"type": "job", "title": "Partly sent", "company": "Stripe"})
    seeded_db.execute("UPDATE outbox SET delivered = ? WHERE id = 1", ('["discord"]',))

    assert "discord" in client.get("/outbox?status=queued").text


# ── §13.8: the UI is an accessory, never a peer ───────────────────────────


def test_a_route_exception_renders_an_error_page_instead_of_propagating(service):
    app = create_app(service)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("deliberate")

    with TestClient(app, raise_server_exceptions=False) as tc:
        response = tc.get("/boom")

    assert response.status_code == 500
    assert "The poller is unaffected" in response.text


async def test_the_scheduler_keeps_running_after_a_route_raises(service, seeded_db):
    """The binding form of §13.8: a broken screen must not stop alerts."""
    app = create_app(service)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("deliberate")

    add_company(seeded_db, "stripe", name="Stripe")
    sid = add_source(seeded_db, "stripe", seeded=True)
    set_postings(seeded_db, sid, [posting("Software Engineer Intern", "1")])

    with TestClient(app, raise_server_exceptions=False) as tc:
        assert tc.get("/boom").status_code == 500

    report = await service.pipeline.run_company([get_source(seeded_db, sid)])
    assert report.alerted == 1


def test_htmx_requests_get_a_plain_error_not_a_full_page(service):
    app = create_app(service)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("deliberate")

    with TestClient(app, raise_server_exceptions=False) as tc:
        response = tc.get("/boom", headers={"HX-Request": "true"})

    assert response.status_code == 500
    assert "<html" not in response.text


# ── static assets are genuinely vendored (§3) ─────────────────────────────


def test_htmx_is_served_locally_not_from_a_cdn(client):
    response = client.get("/static/vendor/htmx.min.js")
    assert response.status_code == 200
    assert len(response.content) > 10_000


def test_no_page_references_an_external_host(client, populated):
    for path in ["/", "/review", "/filters", "/companies", "/health", "/outbox"]:
        body = client.get(path).text
        for host in ("cdn.", "unpkg.com", "googleapis.com", "jsdelivr", "cdnjs"):
            assert host not in body, f"{path} references {host}"


def test_fonts_are_self_hosted(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert "/static/fonts/archivo-narrow-400.woff2" in css.text
    assert client.get("/static/fonts/jetbrains-mono-400.woff2").status_code == 200


def test_layout_targets_a_380px_viewport(client):
    css = client.get("/static/app.css").text
    assert "@media (max-width: 720px)" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css
