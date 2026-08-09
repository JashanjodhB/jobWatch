"""What a notification channel is (§9).

The outbox owns durability: ordering, retry, backoff, and the exactly-once
bookkeeping that makes an alert survive being killed mid-send. It knows nothing
about embeds or MIME. A *channel* owns formatting and transport for exactly one
destination, and is allowed to fail — a failure is a row status, never an
exception that reaches the scheduler.

Adding a channel therefore means writing one class with two members and putting
it in the list; nothing in the pipeline or the scheduler changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from ..db import parse_ts

__all__ = ["Channel", "DeliveryError", "host_of", "humanize_age"]


class DeliveryError(Exception):
    """A send that did not happen.

    `retryable=False` means it will never happen: a deleted webhook, a rejected
    password, a recipient the server refuses. Those fail the row immediately
    rather than burning five attempts against a wall.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Channel(Protocol):
    """One destination. Formats payloads its own way and delivers them."""

    name: str

    @property
    def configured(self) -> bool:
        """False when its credentials are absent. Unconfigured channels are skipped."""

    async def send(self, payloads: list[dict[str, Any]], *, kind: str = "job") -> None:
        """Deliver these payloads as one notification.

        `kind` is the outbox row kind: 'job', 'alarm', or 'digest'. Only
        'digest' is a grouping instruction — the others always arrive with a
        single payload. Raise `DeliveryError` on failure.
        """


def humanize_age(iso: str | None, *, now: datetime | None = None) -> str:
    """'14s', '3m', '2h', '4d' — the one number the whole system minimizes."""
    when = parse_ts(iso)
    if when is None:
        return "—"
    delta = ((now or datetime.now(UTC)) - when).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def host_of(url: str) -> str:
    """The bare host, shown so an alert says which board it came from."""
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url
