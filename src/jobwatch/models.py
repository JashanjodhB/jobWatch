"""Domain types passed between adapters, the pipeline, and the UI."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["Company", "FetchResult", "Job", "RawPosting", "Source"]


class RawPosting(BaseModel):
    """One posting as an adapter understood it, before normalization.

    Every adapter returns these and nothing else. Validation happens here so a
    schema change at the source surfaces as a loud error rather than a silently
    empty list (§13.10).
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str
    url: str
    req_id: str | None = None
    locations: list[str] = []
    # Whatever the source claims about posting date. Display only -- never used
    # to decide newness (§13.3).
    posted_at: str | None = None
    department: str | None = None

    @field_validator("title")
    @classmethod
    def _title_present(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("posting has no title")
        return v.strip()

    @field_validator("url")
    @classmethod
    def _url_absolute(cls, v: str) -> str:
        v = (v or "").strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"apply URL is not absolute: {v!r}")
        return v

    @field_validator("locations", mode="before")
    @classmethod
    def _coerce_locations(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v if x]

    @field_validator("req_id", mode="before")
    @classmethod
    def _coerce_req_id(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        return str(v)


@dataclass(slots=True)
class FetchResult:
    """What one poll of one source produced."""

    postings: list[RawPosting] = field(default_factory=list)
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    adapter: str = ""

    def __len__(self) -> int:
        return len(self.postings)


@dataclass(slots=True)
class Company:
    slug: str
    display_name: str
    tier: str
    enabled: bool = True
    careers_url: str | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Company:
        return cls(
            slug=row["slug"],
            display_name=row["display_name"],
            tier=row["tier"],
            enabled=bool(row["enabled"]),
            careers_url=row["careers_url"],
            notes=row["notes"],
        )


@dataclass(slots=True)
class Source:
    """A row of `sources`, joined with its company's tier and display name."""

    id: int
    company_slug: str
    adapter: str
    adapter_config: dict[str, Any]
    priority: int
    enabled: bool
    fallback_adapter: str | None
    fallback_config: dict[str, Any]
    last_attempt_at: str | None
    last_success_at: str | None
    consecutive_failures: int
    backoff_until: str | None
    using_fallback: bool
    etag: str | None
    last_modified: str | None
    baseline_posting_count: int | None
    seeded: bool
    # denormalized from companies, for scheduling and display
    tier: str = "warm"
    company_name: str = ""
    company_enabled: bool = True

    @property
    def active_adapter(self) -> str:
        """The adapter this poll will actually use, honouring the fallback flip."""
        if self.using_fallback and self.fallback_adapter:
            return self.fallback_adapter
        return self.adapter

    @property
    def active_config(self) -> dict[str, Any]:
        if self.using_fallback and self.fallback_adapter:
            return self.fallback_config
        return self.adapter_config

    @property
    def label(self) -> str:
        return f"{self.company_slug}/{self.adapter}"

    @property
    def min_interval_seconds(self) -> float:
        """Floor on this source's poll interval, regardless of its company's tier.

        Some endpoints cost many requests per poll — a full Workday scan of a
        large tenant is ~46 — and putting those on a 60-second cadence is how a
        residential IP gets blocked (§13.5). Set it in the source's config.
        """
        raw = self.adapter_config.get("min_interval_seconds")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Source:
        keys = row.keys()
        return cls(
            id=row["id"],
            company_slug=row["company_slug"],
            adapter=row["adapter"],
            adapter_config=_json_obj(row["adapter_config"]),
            priority=row["priority"],
            enabled=bool(row["enabled"]),
            fallback_adapter=row["fallback_adapter"],
            fallback_config=_json_obj(row["fallback_config"]),
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            consecutive_failures=row["consecutive_failures"],
            backoff_until=row["backoff_until"],
            using_fallback=bool(row["using_fallback"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            baseline_posting_count=row["baseline_posting_count"],
            seeded=bool(row["seeded"]),
            tier=row["tier"] if "tier" in keys else "warm",
            company_name=row["display_name"] if "display_name" in keys else row["company_slug"],
            company_enabled=bool(row["company_enabled"]) if "company_enabled" in keys else True,
        )


@dataclass(slots=True)
class Job:
    dedup_key: str
    merge_key: str
    company_slug: str
    source_id: int
    title: str
    normalized_title: str
    locations: list[str]
    url: str
    first_seen_at: str
    last_seen_at: str
    classification: str
    req_id: str | None = None
    source_posted_at: str | None = None
    class_source: str | None = None
    category: str | None = None
    alerted_at: str | None = None
    app_status: str = "none"
    app_updated_at: str | None = None
    company_name: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        keys = row.keys()
        return cls(
            dedup_key=row["dedup_key"],
            merge_key=row["merge_key"],
            company_slug=row["company_slug"],
            source_id=row["source_id"],
            title=row["title"],
            normalized_title=row["normalized_title"],
            locations=_json_list(row["locations"]),
            url=row["url"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            classification=row["classification"],
            req_id=row["req_id"],
            source_posted_at=row["source_posted_at"],
            class_source=row["class_source"],
            category=row["category"],
            alerted_at=row["alerted_at"],
            app_status=row["app_status"] or "none",
            app_updated_at=row["app_updated_at"],
            company_name=row["display_name"] if "display_name" in keys else row["company_slug"],
        )


def _json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []
