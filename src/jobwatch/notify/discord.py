"""Discord webhook delivery and embed formatting (§9).

One channel, no account limits, sub-second delivery, and it already runs on the
phone. Embeds are colour-coded by classification: green for a rules/human match,
amber for something that needs review, red for a source alarm.

Rate limit: Discord allows roughly 5 requests per 2 seconds per webhook. We cap
at 2/second and queue the excess, because during an August surge you will hit it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .base import DeliveryError, host_of, humanize_age

__all__ = [
    "DiscordChannel",
    "DiscordError",
    "DiscordSender",
    "build_alarm_embed",
    "build_digest_embed",
    "build_embed",
    "build_job_embed",
    "humanize_age",
]

# §10 token system, as Discord integer colours.
COLOR_CONFIRMED = 0x5EC1A0   # match
COLOR_SIGNAL = 0xF5A524      # review / needs attention
COLOR_ALARM = 0xE5484D       # source failure, drift
COLOR_MUTED = 0x7A8499       # digest wrapper

MAX_EMBEDS_PER_MESSAGE = 10


class DiscordError(DeliveryError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable)
        self.status = status


def build_job_embed(payload: dict[str, Any]) -> dict[str, Any]:
    """One posting, formatted as in §9."""
    is_review = payload.get("classification") == "review"
    marker = "🟠" if is_review else "🟢"
    company = payload.get("company") or payload.get("company_slug") or "?"
    title = payload.get("title") or "(untitled)"

    lines: list[str] = []
    locations = payload.get("locations") or []
    if locations:
        shown = " · ".join(str(x) for x in locations[:4])
        if len(locations) > 4:
            shown += f" · +{len(locations) - 4} more"
        lines.append(shown)

    detected = humanize_age(payload.get("detected_at"))
    via = host_of(payload.get("url") or "")
    lines.append(f"Detected {detected} ago · via {via}")

    if payload.get("ui_url"):
        lines.append(f"[Triage in jobwatch]({payload['ui_url']})")

    embed: dict[str, Any] = {
        "title": f"{marker} {company} · {title}"[:250],
        "url": payload.get("url"),
        "color": COLOR_SIGNAL if is_review else COLOR_CONFIRMED,
        "description": "\n".join(lines)[:2000],
    }
    if is_review:
        embed["footer"] = {"text": "? needs review — one key in the review queue"}
    elif payload.get("category"):
        embed["footer"] = {"text": str(payload["category"])}
    return embed


def build_digest_embed(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Warm and cold tiers accumulate into one embed, flushed on an interval."""
    lines: list[str] = []
    for item in payloads[:25]:
        company = item.get("company") or item.get("company_slug") or "?"
        title = (item.get("title") or "(untitled)")[:90]
        mark = "?" if item.get("classification") == "review" else "·"
        url = item.get("url")
        entry = f"**{company}** {mark} [{title}]({url})" if url else f"**{company}** {mark} {title}"
        locations = item.get("locations") or []
        if locations:
            entry += f"\n{' · '.join(str(x) for x in locations[:3])}"
        lines.append(entry)

    if len(payloads) > 25:
        lines.append(f"…and {len(payloads) - 25} more — see the feed")

    return {
        "title": f"📋 {len(payloads)} new posting{'s' if len(payloads) != 1 else ''}",
        "color": COLOR_MUTED,
        "description": "\n".join(lines)[:4000],
    }


def build_alarm_embed(payload: dict[str, Any]) -> dict[str, Any]:
    """Drift and source-failure alarms, deliberately styled apart from postings (§12)."""
    return {
        "title": f"🔴 {payload.get('title', 'Source alarm')}"[:250],
        "color": COLOR_ALARM,
        "description": str(payload.get("body", ""))[:2000],
        "footer": {"text": payload.get("footer", "jobwatch health")},
    }


def build_embed(payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("type", "job")
    if kind == "alarm":
        return build_alarm_embed(payload)
    return build_job_embed(payload)


@dataclass
class DiscordSender:
    """Posts embeds to one webhook, never faster than `max_per_second`."""

    client: httpx.AsyncClient
    webhook_url: str | None
    max_per_second: float = 2.0
    _last_send: float = 0.0

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    async def send_embeds(self, embeds: list[dict[str, Any]], *, content: str | None = None) -> None:
        if not self.webhook_url:
            raise DiscordError(
                "no webhook configured — set DISCORD_WEBHOOK_URL", retryable=False
            )
        if not embeds:
            return

        for start in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
            chunk = embeds[start : start + MAX_EMBEDS_PER_MESSAGE]
            body: dict[str, Any] = {"embeds": chunk, "allowed_mentions": {"parse": []}}
            if content and start == 0:
                body["content"] = content[:2000]
            await self._post(body)

    async def _post(self, body: dict[str, Any]) -> None:
        await self._throttle()
        try:
            response = await self.client.post(self.webhook_url, json=body, timeout=20.0)
        except httpx.HTTPError as exc:
            raise DiscordError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code == 429:
            retry_after = _retry_after(response)
            await asyncio.sleep(min(retry_after, 30.0))
            raise DiscordError(
                f"rate limited by Discord, retry after {retry_after:.1f}s", status=429
            )

        if response.status_code in (401, 403, 404):
            # A deleted or mistyped webhook will never start working. Fail the row
            # rather than retrying five times into a wall.
            raise DiscordError(
                f"webhook rejected the request (HTTP {response.status_code}) — "
                "the URL is wrong or the webhook was deleted",
                status=response.status_code,
                retryable=False,
            )

        if response.status_code >= 400:
            raise DiscordError(
                f"HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )

    async def _throttle(self) -> None:
        if self.max_per_second <= 0:
            return
        min_gap = 1.0 / self.max_per_second
        async with self._lock:
            elapsed = time.monotonic() - self._last_send
            if elapsed < min_gap:
                await asyncio.sleep(min_gap - elapsed)
            self._last_send = time.monotonic()


@dataclass
class DiscordChannel:
    """Adapts the webhook sender to the outbox's channel contract."""

    sender: DiscordSender
    name: str = "discord"

    @property
    def configured(self) -> bool:
        return bool(self.sender.configured)

    async def send(self, payloads: list[dict[str, Any]], *, kind: str = "job") -> None:
        if not payloads:
            return
        if kind == "digest" and len(payloads) > 1:
            embeds = [build_digest_embed(payloads)]
        else:
            embeds = [build_embed(payload) for payload in payloads]
        await self.sender.send_embeds(embeds)


def _retry_after(response: httpx.Response) -> float:
    try:
        data = response.json()
        if isinstance(data, dict) and "retry_after" in data:
            return float(data["retry_after"])
    except ValueError:
        pass
    try:
        return float(response.headers.get("retry-after", 1.0))
    except (TypeError, ValueError):
        return 1.0
