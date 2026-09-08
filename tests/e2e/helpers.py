"""Seeding and waiting helpers for the browser tier.

Kept out of `conftest.py` so tests import them by an unambiguous module name:
`tests/` and `tests/e2e/` are both on `sys.path` during collection, and two
modules called `conftest` is exactly the ambiguity worth not having.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from jobwatch.db import utcnow
from jobwatch.normalize import dedup_key, merge_key, normalize_title

# Every wait here is explicit. `driver.implicitly_wait(0)` is set once in the
# `driver` fixture and must stay there: mixing an implicit wait with an explicit
# one makes both unpredictable, and the failure mode is a test that passes for
# the wrong reason.
DEFAULT_TIMEOUT = 6.0
POLL = 0.05


# ── waiting ───────────────────────────────────────────────────────────────


def wait_until(driver, condition, *, timeout: float = DEFAULT_TIMEOUT, message: str = ""):
    """Explicit wait with a message that says what was expected, not 'TimeoutException'.

    `StaleElementReferenceException` is ignored alongside the usual
    `NoSuchElementException`, and in a UI built on HTMX swaps that is not a
    nicety: a poll that happens to read a node while its container is being
    replaced hits a stale handle, and without this the test fails intermittently
    on the swap it is waiting for. Note that `ignored_exceptions` *replaces*
    Selenium's default rather than adding to it, so both are listed.
    """
    return WebDriverWait(
        driver,
        timeout,
        poll_frequency=POLL,
        ignored_exceptions=(NoSuchElementException, StaleElementReferenceException),
    ).until(condition, message)


def wait_for_text(driver, selector: str, expected: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Wait until `selector` contains `expected`, reporting what it held instead."""

    def _check(drv) -> bool:
        try:
            return expected in drv.find_element(By.CSS_SELECTOR, selector).text
        except Exception:
            return False

    try:
        wait_until(driver, _check, timeout=timeout)
    except TimeoutException:
        actual = text_of(driver, selector)
        raise AssertionError(
            f"{selector!r} never contained {expected!r}; it held {actual!r}"
        ) from None


def wait_for_selector(driver, selector: str, *, timeout: float = DEFAULT_TIMEOUT):
    """Wait for `selector` to exist, and hand it back.

    Preferred over `wait_for_text` whenever CSS is in play: `.text` is *rendered*
    text, so a `text-transform: uppercase` label comes back uppercased and an
    assertion on the source string fails for a reason that has nothing to do
    with the behaviour under test.
    """
    try:
        return wait_until(
            driver, lambda d: (d.find_elements(By.CSS_SELECTOR, selector) or [None])[0], timeout=timeout
        )
    except TimeoutException:
        raise AssertionError(f"{selector!r} never appeared") from None


def text_of(driver, selector: str) -> str:
    try:
        return driver.find_element(By.CSS_SELECTOR, selector).text
    except Exception:
        return "<no such element>"


# ── the review card ───────────────────────────────────────────────────────


def card_title(driver) -> str:
    return text_of(driver, "#review-card .review-title")


def wait_for_card(driver, expected_title: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """Wait for an HTMX swap to land a specific card in `#review-card`.

    Asserting on the *content* rather than on staleness is deliberate. HTMX
    replaces innerHTML, so a swap into the wrong target still leaves the old
    node stale -- `staleness_of` would go green on the bug this tier exists to
    catch.
    """

    def _check(drv) -> bool:
        return card_title(drv) == expected_title

    try:
        wait_until(driver, _check, timeout=timeout)
    except TimeoutException:
        raise AssertionError(
            f"card never became {expected_title!r}; it shows {card_title(driver)!r}"
        ) from None


def press(driver, key: str, *, ctrl: bool = False) -> None:
    """Send a real keystroke to the document, the way a person would."""
    body = driver.find_element(By.TAG_NAME, "body")
    if ctrl:
        (
            ActionChains(driver)
            .key_down(Keys.CONTROL, body)
            .send_keys(key)
            .key_up(Keys.CONTROL)
            .perform()
        )
    else:
        body.send_keys(key)


# ── seeding ───────────────────────────────────────────────────────────────


def add_company(db, slug: str, *, name: str | None = None, tier: str = "hot") -> None:
    db.execute(
        "INSERT OR IGNORE INTO companies(slug, display_name, tier, enabled) VALUES(?,?,?,1)",
        (slug, name or slug.title(), tier),
    )


def add_source(db, slug: str, adapter: str = "fake") -> int:
    cur = db.execute(
        "INSERT INTO sources(company_slug, adapter, adapter_config, priority, enabled, seeded) "
        "VALUES(?,?,?,1,1,1)",
        (slug, adapter, json.dumps({})),
    )
    return int(cur.lastrowid)


def seed_review_queue(db, titles: list[str], *, company: str = "stripe") -> int:
    """Put `titles` in the review queue, in the order the UI will show them.

    The queue orders by `MAX(first_seen_at) DESC`, so the timestamps descend to
    make the card order deterministic rather than dependent on insert order.
    """
    add_company(db, company, name=company.title())
    source_id = add_source(db, company)
    now = datetime.now(UTC)

    for index, title in enumerate(titles):
        seen = (now - timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = dedup_key(company, "fake", str(index), title)
        db.execute(
            "INSERT OR REPLACE INTO jobs(dedup_key, merge_key, company_slug, source_id, "
            "req_id, title, normalized_title, locations, url, first_seen_at, last_seen_at, "
            "classification, class_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,'review','rules')",
            (
                key,
                merge_key(company, title),
                company,
                source_id,
                str(index),
                title,
                normalize_title(title),
                json.dumps(["Austin, TX"]),
                f"https://example.com/jobs/{index}",
                seen,
                seen,
            ),
        )
    return source_id


def add_filter_rule(db, kind: str, pattern: str, *, note: str = "") -> int:
    cur = db.execute(
        "INSERT INTO filter_rules(kind, pattern, enabled, note, created_at) VALUES(?,?,1,?,?)",
        (kind, pattern, note or None, utcnow()),
    )
    return int(cur.lastrowid)


def enqueue_failed(service, merge: str, payload: dict[str, Any], *, error: str = "boom") -> int:
    """An outbox row in the state the Replay button exists for."""
    row_id = service.outbox.enqueue(merge, payload)
    service.db.execute(
        "UPDATE outbox SET status = 'failed', last_error = ? WHERE id = ?", (error, row_id)
    )
    return int(row_id)


def verdicts(db) -> dict[str, tuple[str, str | None]]:
    """Every recorded human verdict as {normalized_title: (verdict, category)}."""
    return {
        row["normalized_title"]: (row["verdict"], row["category"])
        for row in db.query("SELECT normalized_title, verdict, category FROM title_verdicts")
    }
