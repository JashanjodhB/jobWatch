"""Configuration loading, seeding, and the YAML round trip (§16, Phase 7)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import CONFIG_DIR
from jobwatch.config import (
    CompanySpec,
    FilterSpec,
    Settings,
    export_config,
    load_companies,
    load_env,
    load_filters,
    load_settings,
    seed_database,
)

# ── the shipped config must be valid ──────────────────────────────────────


def test_shipped_settings_load():
    settings = load_settings(CONFIG_DIR / "settings.yaml")
    assert set(settings.tiers) >= {"hot", "warm", "cold"}
    assert settings.tiers["hot"].interval_seconds == 60
    assert settings.notifications.max_per_second <= 2.5, "Discord allows ~5 per 2s"


def test_shipped_registry_loads_and_every_adapter_exists():
    from jobwatch.adapters.base import known_adapters

    companies = load_companies(CONFIG_DIR / "companies.yaml")
    registered = set(known_adapters())
    assert companies

    for company in companies:
        assert company.sources, f"{company.slug} has no sources"
        for source in company.sources:
            assert source.adapter in registered, f"{company.slug}: unknown adapter {source.adapter}"
            if source.fallback_adapter:
                assert source.fallback_adapter in registered


def test_shipped_filters_compile():
    import re

    filters = load_filters(CONFIG_DIR / "filters.yaml")
    assert filters.require_any and filters.role_any and filters.exclude_any
    for _kind, pattern in filters.as_rows():
        re.compile(pattern)  # raises if any shipped pattern is malformed


def test_duplicate_slugs_are_rejected(tmp_path: Path):
    path = tmp_path / "companies.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"slug": "x", "display_name": "X", "sources": []},
                {"slug": "x", "display_name": "X again", "sources": []},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_companies(path)


def test_unknown_keys_are_rejected_rather_than_ignored(tmp_path: Path):
    """A typo in the registry must fail loudly, not silently do nothing."""
    path = tmp_path / "companies.yaml"
    path.write_text(
        yaml.safe_dump([{"slug": "x", "display_name": "X", "teir": "hot", "sources": []}]),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="teir"):
        load_companies(path)


def test_missing_tier_in_settings_is_rejected():
    with pytest.raises(ValueError, match="missing"):
        Settings.model_validate({"tiers": {"hot": {"interval_seconds": 60}}})


# ── seeding ───────────────────────────────────────────────────────────────


@pytest.fixture
def spec() -> list[CompanySpec]:
    return [
        CompanySpec.model_validate(
            {
                "slug": "stripe",
                "display_name": "Stripe",
                "tier": "hot",
                "careers_url": "https://stripe.com/jobs",
                "sources": [
                    {"adapter": "greenhouse", "priority": 1, "config": {"board_token": "stripe"}}
                ],
            }
        )
    ]


def test_seeding_creates_companies_sources_and_rules(db, spec):
    report = seed_database(db, spec, FilterSpec(require_any=[r"\bintern\b"]))

    assert (report.companies_added, report.sources_added, report.rules_added) == (1, 1, 1)
    assert db.scalar("SELECT tier FROM companies WHERE slug = 'stripe'") == "hot"
    assert db.scalar("SELECT adapter FROM sources WHERE company_slug = 'stripe'") == "greenhouse"


def test_seeding_is_idempotent(db, spec):
    filters = FilterSpec(require_any=[r"\bintern\b"])
    seed_database(db, spec, filters)
    second = seed_database(db, spec, filters)

    assert (second.companies_added, second.sources_added, second.rules_added) == (0, 0, 0)
    assert db.scalar("SELECT COUNT(*) FROM sources", default=0) == 1
    assert db.scalar("SELECT COUNT(*) FROM filter_rules", default=0) == 1


def test_reseeding_does_not_undo_ui_edits(db, spec):
    """The UI owns the tables once they exist; a restart must not clobber it."""
    seed_database(db, spec, FilterSpec())
    db.execute("UPDATE companies SET enabled = 0 WHERE slug = 'stripe'")
    db.execute("UPDATE companies SET tier = 'cold' WHERE slug = 'stripe'")

    seed_database(db, spec, FilterSpec())

    assert db.scalar("SELECT enabled FROM companies WHERE slug='stripe'") == 0
    assert db.scalar("SELECT tier FROM companies WHERE slug='stripe'") == "cold"


def test_reseeding_never_resets_the_seeded_flag(db, spec):
    """§13.1: resetting it would replay every posting on the board as new."""
    seed_database(db, spec, FilterSpec())
    db.execute("UPDATE sources SET seeded = 1, consecutive_failures = 3, etag = 'abc'")

    seed_database(db, spec, FilterSpec(), force=True)

    row = db.one("SELECT seeded, consecutive_failures, etag FROM sources")
    assert row["seeded"] == 1
    assert row["consecutive_failures"] == 3
    assert row["etag"] == "abc"


def test_force_updates_descriptive_fields(db, spec):
    seed_database(db, spec, FilterSpec())
    db.execute("UPDATE companies SET display_name = 'Renamed' WHERE slug = 'stripe'")

    seed_database(db, spec, FilterSpec(), force=True)

    assert db.scalar("SELECT display_name FROM companies WHERE slug='stripe'") == "Stripe"


def test_seeding_warns_about_a_company_with_no_sources(db):
    spec = [CompanySpec.model_validate({"slug": "x", "display_name": "X", "sources": []})]
    report = seed_database(db, spec, FilterSpec())
    assert any("no sources" in w for w in report.warnings)


# ── export round trip ─────────────────────────────────────────────────────


def test_export_round_trips_through_the_seeder(db, tmp_path: Path):
    """Phase 7 acceptance: exported YAML re-imports cleanly."""
    companies = load_companies(CONFIG_DIR / "companies.yaml")
    filters = load_filters(CONFIG_DIR / "filters.yaml")
    seed_database(db, companies, filters)

    companies_yaml, filters_yaml = export_config(db)
    (tmp_path / "companies.yaml").write_text(companies_yaml, encoding="utf-8")
    (tmp_path / "filters.yaml").write_text(filters_yaml, encoding="utf-8")

    reloaded = load_companies(tmp_path / "companies.yaml")
    reloaded_filters = load_filters(tmp_path / "filters.yaml")

    assert {c.slug for c in reloaded} == {c.slug for c in companies}
    assert sorted(reloaded_filters.require_any) == sorted(filters.require_any)
    assert sorted(reloaded_filters.exclude_any) == sorted(filters.exclude_any)

    # And it seeds a fresh database to the same shape.
    from jobwatch.db import Database

    fresh = Database(tmp_path / "fresh.db")
    fresh.migrate()
    try:
        seed_database(fresh, reloaded, reloaded_filters)
        assert fresh.scalar("SELECT COUNT(*) FROM companies", default=0) == len(companies)
        assert fresh.scalar("SELECT COUNT(*) FROM sources", default=0) == db.scalar(
            "SELECT COUNT(*) FROM sources", default=0
        )
    finally:
        fresh.close()


def test_export_preserves_disabled_state(db, spec):
    seed_database(db, spec, FilterSpec())
    db.execute("UPDATE companies SET enabled = 0")

    companies_yaml, _ = export_config(db)
    assert "enabled: false" in companies_yaml
    assert yaml.safe_load(companies_yaml)[0]["enabled"] is False


def test_export_omits_disabled_rules(db):
    seed_database(db, [], FilterSpec(require_any=[r"\bintern\b", r"\bco-op\b"]))
    db.execute("UPDATE filter_rules SET enabled = 0 WHERE pattern = '\\bco-op\\b'")

    _, filters_yaml = export_config(db)
    assert yaml.safe_load(filters_yaml)["require_any"] == [r"\bintern\b"]


# ── environment ───────────────────────────────────────────────────────────


def test_env_file_is_loaded_without_overriding_the_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "# a comment\n"
        'DISCORD_WEBHOOK_URL="https://discord.test/hook"\n'
        "HEALTHCHECK_URL=https://hc.test/ping\n"
        "\n"
        "ALREADY_SET=from_file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBWATCH_ENV_FILE", str(env_file))
    monkeypatch.setenv("ALREADY_SET", "from_environment")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    load_env()

    import os

    assert os.environ["DISCORD_WEBHOOK_URL"] == "https://discord.test/hook"
    assert os.environ["HEALTHCHECK_URL"] == "https://hc.test/ping"
    assert os.environ["ALREADY_SET"] == "from_environment"


def test_missing_env_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBWATCH_ENV_FILE", str(tmp_path / "nope.env"))
    load_env()  # must not raise


def test_no_secrets_are_committed():
    """§13.11."""
    root = Path(__file__).parent.parent
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert not (root / ".env").exists() or ".env" in gitignore
