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

Paging (all optional; omitted means a single request):
    page_param      query parameter carrying the page/offset
    page_start      its value on the first page      (default 0)
    page_step       how much it grows per page       (default 10)
    max_pages       hard cap                         (default 12)

`page_step` covers both conventions in one knob: an offset board (Avature's
`jobOffset`) steps by its page size, a page-number board (Google's `page`)
starts at 1 and steps by 1.

A selector that matches nothing raises rather than returning `[]` -- an empty
board and a broken selector look identical downstream, and only one of them is
supposed to be silent (§13.10). On later pages it just means the end of the
results, so only the *first* page treats it as drift.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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

DEFAULT_PAGE_STEP = 10
DEFAULT_MAX_PAGES = 12


def _load_parser():
    try:
        from selectolax.parser import HTMLParser
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise AdapterUnavailable(
            "the `html` adapter needs the 'html' extra: uv sync --extra html",
            adapter="html",
        ) from exc
    return HTMLParser


def _with_param(url: str, param: str, value: int) -> str:
    """Set one query parameter on a URL, replacing any existing value."""
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, str(value)))
    return urlunparse(parts._replace(query=urlencode(query)))


@register("html")
class HtmlAdapter:
    name: ClassVar[str] = "html"

    async def fetch(self, ctx: FetchContext) -> FetchResult:
        HTMLParser = _load_parser()

        url = str(ctx.require("url"))
        job_selector = str(ctx.require("job_selector"))
        base = str(ctx.config.get("base_url") or url)

        page_param = ctx.config.get("page_param")
        page_start = int(ctx.config.get("page_start", 0))
        page_step = int(ctx.config.get("page_step", DEFAULT_PAGE_STEP))
        max_pages = int(ctx.config.get("max_pages", DEFAULT_MAX_PAGES))
        if not page_param:
            max_pages = 1
        if ctx.max_pages is not None:
            max_pages = min(max_pages, ctx.max_pages)

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        etag = last_modified = None

        for page in range(max_pages):
            target = url if not page_param else _with_param(
                url, str(page_param), page_start + page * page_step
            )
            # Conditional headers describe the first page only; sending them on
            # page 2 would compare its ETag against page 1's.
            response = await self._get(ctx, target, conditional=(page == 0))
            if response is None:
                return FetchResult(not_modified=True, adapter=self.name)

            if page == 0:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")

            tree = HTMLParser(response.text)
            nodes = tree.css(job_selector)
            if not nodes:
                if page == 0:
                    raise AdapterError(
                        f"selector {job_selector!r} matched nothing on {target} -- "
                        "the page markup almost certainly changed",
                        adapter=self.name,
                        payload_snippet=response_snippet(response, 200),
                    )
                break  # a later empty page is simply the end of the results

            new_on_page = 0
            for node in nodes:
                mapped = self._map(node, ctx.config, base)
                if mapped is None or mapped["url"] in seen:
                    continue
                seen.add(mapped["url"])
                items.append(mapped)
                new_on_page += 1

            # Boards that clamp the offset re-serve the last page forever.
            if new_on_page == 0:
                break

        if not items:
            raise AdapterError(
                f"selector {job_selector!r} matched nodes but none had "
                "a usable link and title",
                adapter=self.name,
            )

        return FetchResult(
            postings=build_postings(items, self.name),
            etag=etag,
            last_modified=last_modified,
            adapter=self.name,
        )

    async def _get(self, ctx: FetchContext, url: str, *, conditional: bool):
        """One GET. Returns None if the source answered 304 Not Modified."""
        import httpx

        headers = {"Accept": "text/html,application/xhtml+xml"}
        if conditional:
            headers.update(conditional_headers(ctx.etag, ctx.last_modified))
        headers.update(ctx.extra_headers)

        try:
            response = await ctx.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterError(f"{type(exc).__name__}: {exc}", adapter=self.name) from exc

        if response.status_code == 304:
            return None
        raise_for_status(response, self.name)
        return response

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
