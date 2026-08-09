"""Playwright fallback — last resort only (§11).

**Intercepts the XHR response rather than scraping the DOM.** A rendered page is
a moving target; the JSON the page fetched to build itself is the same shape the
`direct` adapter would have used, and it survives cosmetic redesigns.

Needs the `browser` extra plus a downloaded browser binary:

    uv sync --extra browser
    uv run playwright install chromium

Without it the source is skipped with a clear log line and the service starts
normally (Phase 10 acceptance).

Config:
    url              page to open                                  (required)
    xhr_contains     substring identifying the postings request     (recommended)
    items_path       dot path to the array inside that JSON         (optional)
    field_map        {title|url|req_id|locations|...: dot path}     (optional)
    base_url         base for relative apply paths                  (optional)
    wait_ms          extra settle time after networkidle            (default 2500)
    job_selector     DOM fallback if no XHR matched                 (optional)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, ClassVar

from ..models import FetchResult
from .base import (
    AdapterError,
    AdapterUnavailable,
    FetchContext,
    absolute_url,
    build_postings,
    register,
)

DEFAULT_WAIT_MS = 2500
NAV_TIMEOUT_MS = 45_000


def _load_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AdapterUnavailable(
            "the `browser` adapter needs the 'browser' extra: "
            "uv sync --extra browser && uv run playwright install chromium",
            adapter="browser",
        ) from exc
    return async_playwright


@register("browser")
class BrowserAdapter:
    name: ClassVar[str] = "browser"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        async_playwright = _load_playwright()

        url = str(ctx.require("url"))
        xhr_contains = ctx.config.get("xhr_contains")
        wait_ms = int(ctx.config.get("wait_ms", DEFAULT_WAIT_MS))
        captured: list[Any] = []

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=ctx.config.get("user_agent"),
                        viewport={"width": 1440, "height": 1000},
                    )
                    page = await context.new_page()

                    if xhr_contains:
                        page.on("response", _capture(captured, str(xhr_contains)))

                    await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                    # A page that polls in the background never goes idle; that
                    # is fine, the fixed settle below covers it.
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    await asyncio.sleep(wait_ms / 1000)

                    html = await page.content()
                finally:
                    await browser.close()
        except AdapterUnavailable:
            raise
        except Exception as exc:
            raise AdapterError(
                f"browser run failed: {type(exc).__name__}: {exc}", adapter=self.name
            ) from exc

        if captured:
            items = self._from_xhr(captured, ctx.config, url)
            if items:
                return FetchResult(postings=build_postings(items, self.name), adapter=self.name)

        if ctx.config.get("job_selector"):
            return await self._from_dom(html, ctx, url)

        raise AdapterError(
            f"no XHR matching {xhr_contains!r} was seen and no job_selector is "
            "configured -- nothing to parse",
            adapter=self.name,
        )

    # -- XHR path ----------------------------------------------------------

    def _from_xhr(
        self, payloads: list[Any], config: dict[str, Any], page_url: str
    ) -> list[dict[str, Any]]:
        items_path = config.get("items_path")
        field_map = config.get("field_map") or {}
        base = str(config.get("base_url") or page_url)

        rows: list[Any] = []
        for payload in payloads:
            node = dig(payload, items_path) if items_path else payload
            if isinstance(node, list):
                rows.extend(node)

        if not rows:
            raise AdapterError(
                f"captured {len(payloads)} XHR response(s) but found no array at "
                f"items_path={items_path!r}",
                adapter=self.name,
                payload_snippet=json.dumps(payloads[0])[:300] if payloads else "",
            )

        mapped: list[dict[str, Any]] = []
        for row in rows:
            title = dig(row, field_map.get("title", "title"))
            href = dig(row, field_map.get("url", "url"))
            if not title or not href:
                continue
            locations = dig(row, field_map["locations"]) if "locations" in field_map else None
            mapped.append(
                {
                    "req_id": dig(row, field_map.get("req_id", "id")),
                    "title": str(title),
                    "url": absolute_url(base, str(href)),
                    "locations": locations if isinstance(locations, list) else _one(locations),
                    "posted_at": dig(row, field_map["posted_at"]) if "posted_at" in field_map else None,
                    "department": None,
                }
            )
        return mapped

    # -- DOM path ----------------------------------------------------------

    async def _from_dom(self, html: str, ctx: FetchContext, url: str) -> FetchResult:
        from .html import HtmlAdapter, _load_parser

        HTMLParser = _load_parser()
        tree = HTMLParser(html)
        selector = str(ctx.config["job_selector"])
        nodes = tree.css(selector)
        if not nodes:
            raise AdapterError(
                f"job_selector {selector!r} matched nothing in the rendered page",
                adapter=self.name,
            )

        helper = HtmlAdapter()
        base = str(ctx.config.get("base_url") or url)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in nodes:
            mapped = helper._map(node, ctx.config, base)
            if mapped and mapped["url"] not in seen:
                seen.add(mapped["url"])
                items.append(mapped)

        if not items:
            raise AdapterError(
                f"job_selector {selector!r} matched {len(nodes)} nodes but none were usable",
                adapter=self.name,
            )
        return FetchResult(postings=build_postings(items, self.name), adapter=self.name)


def _capture(sink: list[Any], needle: str):
    async def on_response(response: Any) -> None:
        if needle not in response.url:
            return
        try:
            sink.append(await response.json())
        except Exception:
            return

    def handler(response: Any) -> None:
        asyncio.ensure_future(on_response(response))  # noqa: RUF006

    return handler


def dig(obj: Any, path: str | None) -> Any:
    """Walk a dot path through nested dicts and lists: 'data.jobs.0.title'."""
    if path is None:
        return None
    node = obj
    for part in str(path).split("."):
        if node is None:
            return None
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _one(value: Any) -> list[str]:
    return [str(value)] if value else []
