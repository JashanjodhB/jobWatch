"""Configuration models, YAML loading, environment secrets, and DB seeding (§16).

Lifecycle: `config/*.yaml` seeds SQLite on first run; after that the database is
authoritative and the web UI owns it. Seeding is therefore **additive** -- it
inserts rows that do not exist and leaves existing rows alone, so restarting the
service never clobbers a company you disabled or a rule you tuned in the UI.
`jobwatch export-config` writes the database back out to YAML for git.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import Database, utcnow

if TYPE_CHECKING:
    from .notify.email import EmailConfig

__all__ = [
    "AppConfig",
    "CompanySpec",
    "EmailSettings",
    "FilterSpec",
    "SeedReport",
    "Settings",
    "SourceSpec",
    "config_dir",
    "load_companies",
    "load_env",
    "load_filters",
    "load_settings",
    "seed_database",
]

TIERS = ("hot", "warm", "cold")


def config_dir() -> Path:
    """JOBWATCH_CONFIG_DIR if set, else ./config."""
    env = os.environ.get("JOBWATCH_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.cwd() / "config"


# ── settings.yaml ─────────────────────────────────────────────────────────


class TierSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(gt=0)
    batch_notifications: bool = False


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Per-request timeout, enforced by httpx.
    timeout_seconds: float = 20.0
    # Whole-fetch deadline, enforced by the pipeline. A paginating adapter makes
    # many requests -- a full Workday scan of a large tenant is ~50 -- so this
    # cannot be derived from the per-request timeout. Its job is to catch an
    # adapter that has hung, not to cap legitimate pagination.
    fetch_deadline_seconds: float = 300.0
    max_concurrency: int = 12
    user_agent: str = "jobwatch/2.0 (personal internship monitor)"
    backoff_base_seconds: float = 30.0
    backoff_max_seconds: float = 3600.0
    fallback_after_failures: int = 5


class ClassificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_review: Literal["alert", "queue_only"] = "alert"


class EmailSettings(BaseModel):
    """Presentation and timing for the email channel.

    The addresses and the password are deliberately not here: they live in the
    environment, because two of them are secrets and the rest are useless
    without those two. See `notify.email.EmailConfig.from_env`.
    """

    model_config = ConfigDict(extra="forbid")
    # Off switch that does not require deleting working credentials.
    enabled: bool = True
    subject_prefix: str = "[jobwatch]"
    timeout_seconds: float = 30.0


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discord_webhook_env: str = "DISCORD_WEBHOOK_URL"
    max_per_second: float = 2.0
    digest_interval_minutes: int = 30
    max_attempts: int = 5
    email: EmailSettings = EmailSettings()


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    public_base_url: str = ""

    @property
    def base_url(self) -> str:
        return self.public_base_url or f"http://{self.bind_host}:{self.bind_port}"


class RetentionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_log_days: int = 30


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: dict[str, TierSettings]
    seasonal_multipliers: dict[str, float] = Field(default_factory=lambda: {"default": 1.0})
    http: HttpSettings = HttpSettings()
    classification: ClassificationSettings = ClassificationSettings()
    notifications: NotificationSettings = NotificationSettings()
    web: WebSettings = WebSettings()
    retention: RetentionSettings = RetentionSettings()

    @field_validator("seasonal_multipliers", mode="before")
    @classmethod
    def _stringify_month_keys(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {str(k): float(x) for k, x in v.items()}
        return v

    @field_validator("tiers")
    @classmethod
    def _known_tiers(cls, v: dict[str, TierSettings]) -> dict[str, TierSettings]:
        missing = set(TIERS) - set(v)
        if missing:
            raise ValueError(f"settings.tiers is missing: {sorted(missing)}")
        return v

    def seasonal_multiplier(self, when: datetime | None = None) -> float:
        month = (when or datetime.now()).month
        return self.seasonal_multipliers.get(
            str(month), self.seasonal_multipliers.get("default", 1.0)
        )

    def interval_for(self, tier: str, when: datetime | None = None) -> float:
        """Poll interval in seconds for a tier, scaled for the season (§16)."""
        base = self.tiers.get(tier) or self.tiers["warm"]
        return base.interval_seconds * self.seasonal_multiplier(when)

    def batches(self, tier: str) -> bool:
        cfg = self.tiers.get(tier)
        return bool(cfg and cfg.batch_notifications)


# ── companies.yaml ────────────────────────────────────────────────────────


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: str
    priority: int = 1
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    fallback_adapter: str | None = None
    fallback_config: dict[str, Any] = Field(default_factory=dict)


class CompanySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    tier: Literal["hot", "warm", "cold"] = "warm"
    enabled: bool = True
    careers_url: str | None = None
    notes: str | None = None
    sources: list[SourceSpec] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or " " in v:
            raise ValueError(f"invalid company slug: {v!r}")
        return v


# ── filters.yaml ──────────────────────────────────────────────────────────


class FilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_any: list[str] = Field(default_factory=list)
    role_any: list[str] = Field(default_factory=list)
    exclude_any: list[str] = Field(default_factory=list)
    location_exclude: list[str] = Field(default_factory=list)

    def as_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for kind in ("require_any", "role_any", "exclude_any", "location_exclude"):
            rows.extend((kind, pattern) for pattern in getattr(self, kind))
        return rows


# ── loading ───────────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate(_read_yaml(path or config_dir() / "settings.yaml"))


def load_companies(path: Path | None = None) -> list[CompanySpec]:
    data = _read_yaml(path or config_dir() / "companies.yaml") or []
    if not isinstance(data, list):
        raise ValueError("companies.yaml must be a list of company blocks")
    companies = [CompanySpec.model_validate(item) for item in data]
    slugs = [c.slug for c in companies]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    if dupes:
        raise ValueError(f"duplicate company slugs in companies.yaml: {sorted(dupes)}")
    return companies


def load_filters(path: Path | None = None) -> FilterSpec:
    return FilterSpec.model_validate(_read_yaml(path or config_dir() / "filters.yaml") or {})


def load_env(extra_paths: list[Path] | None = None) -> None:
    """Populate os.environ from an env file. Existing variables always win.

    Looks at JOBWATCH_ENV_FILE, then ./.env, then /etc/jobwatch.env -- the last
    being where the secret actually lives on the server (§16).
    """
    candidates: list[Path] = []
    if extra_paths:
        candidates.extend(extra_paths)
    explicit = os.environ.get("JOBWATCH_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path("/etc/jobwatch.env"))

    for path in candidates:
        try:
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


@dataclass(slots=True)
class AppConfig:
    """Everything the running service needs, resolved once at startup."""

    settings: Settings
    db_path: Path
    config_dir: Path
    discord_webhook: str | None = None
    email: EmailConfig | None = None
    healthcheck_url: str | None = None
    dry_run: bool = False

    @classmethod
    def load(cls, cfg_dir: Path | None = None, db_path: Path | None = None) -> AppConfig:
        from .db import default_db_path
        from .notify.email import EmailConfig

        load_env()
        cdir = cfg_dir or config_dir()
        settings = load_settings(cdir / "settings.yaml")
        mail = settings.notifications.email
        return cls(
            settings=settings,
            db_path=db_path or default_db_path(),
            config_dir=cdir,
            discord_webhook=os.environ.get(settings.notifications.discord_webhook_env) or None,
            # None when SMTP_HOST/EMAIL_TO are unset, which is the normal state
            # for someone running on Discord alone.
            email=(
                EmailConfig.from_env(
                    subject_prefix=mail.subject_prefix, timeout_seconds=mail.timeout_seconds
                )
                if mail.enabled
                else None
            ),
            healthcheck_url=os.environ.get("HEALTHCHECK_URL") or None,
        )


# ── seeding ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SeedReport:
    companies_added: int = 0
    sources_added: int = 0
    rules_added: int = 0
    companies_updated: int = 0
    sources_updated: int = 0
    skipped_existing: int = 0
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"companies +{self.companies_added}/~{self.companies_updated}  "
            f"sources +{self.sources_added}/~{self.sources_updated}  "
            f"rules +{self.rules_added}"
        )


def seed_database(
    db: Database,
    companies: list[CompanySpec],
    filters: FilterSpec,
    *,
    force: bool = False,
) -> SeedReport:
    """Import YAML config into the database.

    Additive by default: existing companies, sources, and rules are left exactly
    as they are, because the UI owns them once they exist. `force=True` also
    overwrites descriptive fields (name, tier, careers URL, adapter config) from
    YAML -- but never scheduling state, and never the `seeded` flag, because
    resetting that would re-alert every posting on the board (§13.1).
    """
    report = SeedReport()
    now = utcnow()

    with db.tx() as conn:
        for spec in companies:
            existing = conn.execute(
                "SELECT slug FROM companies WHERE slug = ?", (spec.slug,)
            ).fetchone()

            if existing is None:
                conn.execute(
                    "INSERT INTO companies(slug, display_name, tier, enabled, careers_url, notes) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        spec.slug,
                        spec.display_name,
                        spec.tier,
                        int(spec.enabled),
                        spec.careers_url,
                        spec.notes,
                    ),
                )
                report.companies_added += 1
            elif force:
                conn.execute(
                    "UPDATE companies SET display_name=?, tier=?, careers_url=?, notes=? "
                    "WHERE slug=?",
                    (spec.display_name, spec.tier, spec.careers_url, spec.notes, spec.slug),
                )
                report.companies_updated += 1
            else:
                report.skipped_existing += 1

            for src in spec.sources:
                row = conn.execute(
                    "SELECT id FROM sources WHERE company_slug = ? AND adapter = ?",
                    (spec.slug, src.adapter),
                ).fetchone()

                if row is None:
                    conn.execute(
                        "INSERT INTO sources("
                        " company_slug, adapter, adapter_config, priority, enabled,"
                        " fallback_adapter, fallback_config) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            spec.slug,
                            src.adapter,
                            json.dumps(src.config, sort_keys=True),
                            src.priority,
                            int(src.enabled),
                            src.fallback_adapter,
                            json.dumps(src.fallback_config, sort_keys=True),
                        ),
                    )
                    report.sources_added += 1
                elif force:
                    # Scheduling state and `seeded` are deliberately untouched.
                    conn.execute(
                        "UPDATE sources SET adapter_config=?, priority=?, "
                        "fallback_adapter=?, fallback_config=? WHERE id=?",
                        (
                            json.dumps(src.config, sort_keys=True),
                            src.priority,
                            src.fallback_adapter,
                            json.dumps(src.fallback_config, sort_keys=True),
                            row["id"],
                        ),
                    )
                    report.sources_updated += 1

            if not spec.sources:
                report.warnings.append(f"{spec.slug}: no sources configured")

        for kind, pattern in filters.as_rows():
            cur = conn.execute(
                "INSERT OR IGNORE INTO filter_rules(kind, pattern, enabled, created_at) "
                "VALUES(?,?,1,?)",
                (kind, pattern, now),
            )
            report.rules_added += cur.rowcount or 0

    return report


def export_config(db: Database) -> tuple[str, str]:
    """Serialize the database's companies and filter rules back to YAML text."""
    companies: list[dict[str, Any]] = []
    for crow in db.query("SELECT * FROM companies ORDER BY tier, slug"):
        block: dict[str, Any] = {
            "slug": crow["slug"],
            "display_name": crow["display_name"],
            "tier": crow["tier"],
        }
        if not crow["enabled"]:
            block["enabled"] = False
        if crow["careers_url"]:
            block["careers_url"] = crow["careers_url"]
        if crow["notes"]:
            block["notes"] = crow["notes"]

        sources: list[dict[str, Any]] = []
        for srow in db.query(
            "SELECT * FROM sources WHERE company_slug = ? ORDER BY priority, adapter",
            (crow["slug"],),
        ):
            entry: dict[str, Any] = {
                "adapter": srow["adapter"],
                "priority": srow["priority"],
                "config": json.loads(srow["adapter_config"] or "{}"),
            }
            if not srow["enabled"]:
                entry["enabled"] = False
            if srow["fallback_adapter"]:
                entry["fallback_adapter"] = srow["fallback_adapter"]
                fb = json.loads(srow["fallback_config"] or "{}")
                if fb:
                    entry["fallback_config"] = fb
            sources.append(entry)
        block["sources"] = sources
        companies.append(block)

    filters: dict[str, list[str]] = {
        k: [] for k in ("require_any", "role_any", "exclude_any", "location_exclude")
    }
    for row in db.query(
        "SELECT kind, pattern FROM filter_rules WHERE enabled = 1 ORDER BY kind, id"
    ):
        filters[row["kind"]].append(row["pattern"])

    dump = lambda obj: yaml.safe_dump(  # noqa: E731
        obj, sort_keys=False, allow_unicode=True, width=100, default_flow_style=False
    )
    header = "# Generated by `jobwatch export-config`. Edit the UI, then re-export.\n"
    return header + dump(companies), header + dump(filters)
