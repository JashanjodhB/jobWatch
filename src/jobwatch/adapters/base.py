"""Adapter protocol, registry, and error type (§11).

Every adapter takes a `FetchContext` and returns a `FetchResult`. The one rule
that matters: **never swallow a parse error into an empty list** (§13.10). An
adapter that returns `[]` because the JSON shape changed looks exactly like a
company with no open internships, and it will fail silently for months. Raise
`AdapterError` with a payload snippet instead, and let drift detection catch it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable
from urllib.parse import urljoin, urlparse

import httpx
import pydantic

from ..models import FetchResult, RawPosting

__all__ = [
    "AdapterError",
    "AdapterUnavailable",
    "FetchContext",
    "SourceAdapter",
    "absolute_url",
    "build_postings",
    "default_fallback",
    "get_adapter",
    "known_adapters",
    "register",
]


class AdapterError(Exception):
    """A source responded, but not in a shape this adapter understands."""

    def __init__(
        self,
        message: str,
        *,
        adapter: str | None = None,
        payload_snippet: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.adapter = adapter
        self.payload_snippet = payload_snippet
        self.status_code = status_code

    def __str__(self) -> str:
        parts = [self.message]
        if self.adapter:
            parts.insert(0, f"[{self.adapter}]")
        if self.status_code:
            parts.append(f"(HTTP {self.status_code})")
        if self.payload_snippet:
            parts.append(f"payload: {self.payload_snippet}")
        return " ".join(parts)


class AdapterUnavailable(AdapterError):
    """An optional extra this adapter needs is not installed.

    Distinct from AdapterError because the scheduler skips these with a clear log
    line rather than counting them as failures -- a missing extra degrades one
    adapter, it never crashes the process (§3).
    """


@dataclass(slots=True)
class FetchContext:
    """Everything an adapter is allowed to know about the poll it is doing."""

    client: httpx.AsyncClient
    config: dict[str, Any]
    company_slug: str = ""
    adapter_name: str = ""
    etag: str | None = None
    last_modified: str | None = None
    source_id: int | None = None
    # Set by `jobwatch test` so a manual probe never writes conditional state.
    probe: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)

    def require(self, key: str) -> Any:
        """Read a mandatory config key, failing with a message that names the source."""
        if key not in self.config or self.config[key] in (None, ""):
            raise AdapterError(
                f"missing required config key {key!r}",
                adapter=self.adapter_name or "?",
            )
        return self.config[key]


@runtime_checkable
class SourceAdapter(Protocol):
    """The whole contract. One method."""

    name: ClassVar[str]

    async def fetch(self, ctx: FetchContext) -> FetchResult: ...


# ── registry ──────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type] = {}


def register(name: str) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        cls.name = name  # type: ignore[attr-defined]
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_adapter(name: str) -> SourceAdapter:
    """Instantiate a registered adapter by name, e.g. 'greenhouse', 'direct.amazon'."""
    from . import ensure_loaded

    ensure_loaded()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise AdapterError(f"unknown adapter {name!r}", adapter=name)
    return cls()  # type: ignore[return-value]


def known_adapters() -> list[str]:
    from . import ensure_loaded

    ensure_loaded()
    return sorted(_REGISTRY)


# The §11 fallback matrix, used when a source has no explicit fallback configured.
_DEFAULT_FALLBACKS: dict[str, str | None] = {
    "greenhouse": "html",
    "lever": "html",
    "ashby": "html",
    "smartrecruiters": "html",
    "workday": "browser",
    "html": "browser",
    "browser": None,
}


def default_fallback(adapter: str) -> str | None:
    if adapter.startswith("direct."):
        return "html"
    return _DEFAULT_FALLBACKS.get(adapter)


# ── shared helpers ────────────────────────────────────────────────────────


def absolute_url(base: str, path: str | None) -> str:
    """Join a possibly-relative apply path onto its base.

    Several endpoints return a bare path fragment (`externalPath` on Workday,
    for one). Sending a relative URL to Discord produces an unclickable alert,
    which is the most annoying possible way to fail.

    Root-relative paths resolve against the *origin*, not the base's directory:
    `/jobs/1` on `https://x.com/careers` is `https://x.com/jobs/1`, not
    `https://x.com/careers/jobs/1`.
    """
    if not path:
        return base
    path = str(path).strip()
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("//"):
        return f"{urlparse(base).scheme or 'https'}:{path}"
    if path.startswith("/"):
        return urljoin(base, path)
    return urljoin(base.rstrip("/") + "/", path)


def build_postings(items: list[dict[str, Any]], adapter: str) -> list[RawPosting]:
    """Validate a list of already-mapped posting dicts, or raise with context."""
    postings: list[RawPosting] = []
    for index, item in enumerate(items):
        try:
            postings.append(RawPosting.model_validate(item))
        except pydantic.ValidationError as exc:
            raise AdapterError(
                f"posting {index} failed validation: {exc.errors()[0].get('msg', exc)}",
                adapter=adapter,
                payload_snippet=repr(item)[:300],
            ) from exc
    return postings


def require_list(payload: Any, key: str | None, adapter: str) -> list[Any]:
    """Pull the postings array out of a payload, raising if the shape moved.

    This is the single most important guard in the codebase. A renamed field is
    the normal way these endpoints break, and it must be loud.
    """
    node = payload if key is None else (payload or {}).get(key) if isinstance(payload, dict) else None

    if node is None:
        available = sorted(payload)[:12] if isinstance(payload, dict) else type(payload).__name__
        raise AdapterError(
            f"expected a list under {key!r}; response has no such field",
            adapter=adapter,
            payload_snippet=f"top-level keys: {available}",
        )
    if not isinstance(node, list):
        raise AdapterError(
            f"expected {key!r} to be a list, got {type(node).__name__}",
            adapter=adapter,
            payload_snippet=repr(node)[:300],
        )
    return node


def raise_for_status(response: httpx.Response, adapter: str) -> None:
    if response.status_code >= 400:
        from ..http import response_snippet

        hint = {
            401: "requires auth -- this source does not belong in the registry (§2)",
            403: "blocked; likely needs the browser fallback",
            404: "board token / tenant / site is wrong",
            429: "rate limited -- back off harder",
        }.get(response.status_code, "")
        raise AdapterError(
            f"HTTP {response.status_code} {hint}".strip(),
            adapter=adapter,
            payload_snippet=response_snippet(response),
            status_code=response.status_code,
        )


async def conditional_json(
    ctx: FetchContext,
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    use_conditional: bool = True,
) -> tuple[Any, httpx.Response] | tuple[None, httpx.Response]:
    """Issue one request and parse JSON, honouring 304.

    Returns `(None, response)` when the source replied 304 Not Modified, which
    the caller turns into `FetchResult(not_modified=True)` -- no reparse, no
    re-dedup, no work at all (§7).
    """
    from ..http import conditional_headers, json_or_raise

    request_headers: dict[str, str] = {"Accept": "application/json"}
    if use_conditional:
        request_headers.update(conditional_headers(ctx.etag, ctx.last_modified))
    request_headers.update(ctx.extra_headers)
    if headers:
        request_headers.update(headers)

    try:
        if method.upper() == "POST":
            request_headers.setdefault("Content-Type", "application/json")
            response = await ctx.client.post(url, json=json_body, headers=request_headers)
        else:
            response = await ctx.client.get(url, headers=request_headers)
    except httpx.HTTPError as exc:
        raise AdapterError(
            f"{type(exc).__name__}: {exc}", adapter=ctx.adapter_name or "?"
        ) from exc

    if response.status_code == 304:
        return None, response

    raise_for_status(response, ctx.adapter_name or "?")
    return json_or_raise(response, ctx.adapter_name or "?"), response


# ── FakeAdapter: the Phase 0 test double ──────────────────────────────────


@register("fake")
class FakeAdapter:
    """Returns whatever `config['postings']` holds. No network.

    Used by the test suite and by `--dry-run` walkthroughs so the entire
    pipeline can be exercised without touching a real careers page.
    """

    name: ClassVar[str] = "fake"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        if ctx.config.get("raise"):
            raise AdapterError(str(ctx.config["raise"]), adapter=self.name)
        if ctx.config.get("not_modified"):
            return FetchResult(not_modified=True, adapter=self.name)
        items = ctx.config.get("postings", [])
        if not isinstance(items, list):
            raise AdapterError("config['postings'] must be a list", adapter=self.name)
        return FetchResult(
            postings=build_postings(list(items), self.name),
            etag=ctx.config.get("etag"),
            adapter=self.name,
        )
