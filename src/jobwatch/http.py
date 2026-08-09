"""Shared HTTP client and polite-request helpers (§13.5).

Requests come from a residential IP that cannot be rotated, so getting blocked
is effectively unrecoverable. Everything here exists to avoid that: one pooled
client, a hard concurrency cap, an honest User-Agent, real timeouts, and
conditional requests so an unchanged board costs a 304 instead of a payload.
"""

from __future__ import annotations

import random
from typing import Any

import httpx

from .config import HttpSettings

__all__ = [
    "NOT_MODIFIED",
    "backoff_delay",
    "build_client",
    "conditional_headers",
]

NOT_MODIFIED = 304


def build_client(http: HttpSettings) -> httpx.AsyncClient:
    """One client for the whole process: connection reuse, HTTP/2, real timeouts."""
    timeout = httpx.Timeout(
        timeout=http.timeout_seconds,
        connect=min(10.0, http.timeout_seconds),
        read=http.timeout_seconds,
        write=http.timeout_seconds,
        pool=http.timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=max(http.max_concurrency * 2, 16),
        max_keepalive_connections=http.max_concurrency,
        keepalive_expiry=30.0,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        http2=True,
        headers={
            "User-Agent": http.user_agent,
            "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
            # Deliberately no Accept-Encoding: httpx advertises exactly the
            # codecs it can decode. Overriding it advertises brotli, which it
            # cannot decode without an extra, and the body arrives as binary.
        },
    )


def conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def backoff_delay(
    consecutive_failures: int,
    base_seconds: float,
    max_seconds: float,
    *,
    jitter: float = 0.3,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with full-ish jitter.

    Jitter matters more than the curve: without it, thirty sources that failed
    during the same network outage all retry in the same instant and the recovery
    looks exactly like an attack (§13.5).
    """
    if consecutive_failures <= 0:
        return 0.0
    raw = min(base_seconds * (2 ** (consecutive_failures - 1)), max_seconds)
    r = rng or random
    spread = raw * jitter
    return max(1.0, raw + r.uniform(-spread, spread))


def response_snippet(response: httpx.Response | None, limit: int = 300) -> str:
    """A short, safe excerpt of a response body for error messages."""
    if response is None:
        return ""
    try:
        text = response.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return f"<{len(response.content)} bytes of non-text>"
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def looks_like_json(response: httpx.Response) -> bool:
    ctype = response.headers.get("content-type", "")
    return "json" in ctype.lower()


def json_or_raise(response: httpx.Response, adapter: str) -> Any:
    """Parse a JSON body, or raise AdapterError with enough context to debug it."""
    from .adapters.base import AdapterError

    if not looks_like_json(response):
        raise AdapterError(
            f"expected JSON, got {response.headers.get('content-type', 'unknown')!r}",
            adapter=adapter,
            payload_snippet=response_snippet(response),
            status_code=response.status_code,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AdapterError(
            f"response was not valid JSON: {exc}",
            adapter=adapter,
            payload_snippet=response_snippet(response),
            status_code=response.status_code,
        ) from exc
