"""Generic CSS-selector fallback (§11).

Used when a board has no usable JSON endpoint, or as fallback 1 for an adapter
whose primary broke. Needs the `html` extra (`selectolax`); without it the
source is skipped with a clear log line rather than crashing the process.

Config:
    url             page to fetch                                (required)
    job_selector    CSS selector for each posting row or link    (required)
    title_selector  CSS selector for the title, within the row   (optional)
    location_selector                                            (optional)
    link_selector   CSS selector for the <a>, within the row     (optional)
    base_url        base for relative hrefs; defaults to `url`   (optional)

A selector that matches nothing raises rather than returning `[]` -- an empty
board and a broken selector look identical downstream, and only one of them is
supposed to be silent (§13.10).
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..http import conditional_headers, response_snippet
from ..models import FetchResult
from .base import (
    AdapterError,
    AdapterUnavailable,
    FetchContext,
    absolute_url,
    build_postings,
    raise_for_status,
    register,
)


def _load_parser():
    try:
        from selectolax.parser import HTMLParser
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AdapterUnavailable(
            "the `html` adapter needs the 'html' extra: uv sync --extra html",
            adapter="html",
        ) from exc
    return HTMLParser


@register("html")
class HtmlAdapter:
    name: ClassVar[str] = "html"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        HTMLParser = _load_parser()

        url = str(ctx.require("url"))
        job_selector = str(ctx.require("job_selector"))
        base = str(ctx.config.get("base_url") or url)

        headers = {"Accept": "text/html,application/xhtml+xml"}
        headers.update(conditional_headers(ctx.etag, ctx.last_modified))
        headers.update(ctx.extra_headers)

        import httpx

        try:
            response = await ctx.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterError(f"{type(exc).__name__}: {exc}", adapter=self.name) from exc

        if response.status_code == 304:
            return FetchResult(not_modified=True, adapter=self.name)
        raise_for_status(response, self.name)

        tree = HTMLParser(response.text)
        nodes = tree.css(job_selector)
        if not nodes:
            raise AdapterError(
                f"selector {job_selector!r} matched nothing on {url} -- "
                "the page markup almost certainly changed",
                adapter=self.name,
                payload_snippet=response_snippet(response, 200),
            )

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in nodes:
            mapped = self._map(node, ctx.config, base)
            if mapped is None or mapped["url"] in seen:
                continue
            seen.add(mapped["url"])
            items.append(mapped)

        if not items:
            raise AdapterError(
                f"selector {job_selector!r} matched {len(nodes)} nodes but none had "
                "a usable link and title",
                adapter=self.name,
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            adapter=self.name,
        )

    def _map(self, node: Any, config: dict[str, Any], base: str) -> dict[str, Any] | None:
        link_selector = config.get("link_selector")
        anchor = node
        if link_selector:
            anchor = node.css_first(link_selector)
        elif node.tag != "a":
            anchor = node.css_first("a[href]")

        href = anchor.attributes.get("href") if anchor is not None else None
        if not href:
            return None

        title_node = node.css_first(config["title_selector"]) if config.get("title_selector") else None
        title = _text(title_node) or _text(anchor) or _text(node)
        if not title:
            return None

        location_node = (
            node.css_first(config["location_selector"]) if config.get("location_selector") else None
        )
        locations = [_text(location_node)] if _text(location_node) else []

        return {
            "req_id": None,  # HTML boards rarely expose one; the synthetic key covers it
            "title": title,
            "url": absolute_url(base, href),
            "locations": locations,
            "posted_at": None,
            "department": None,
        }


def _text(node: Any) -> str:
    if node is None:
        return ""
    return " ".join(node.text(separator=" ", strip=True).split())
