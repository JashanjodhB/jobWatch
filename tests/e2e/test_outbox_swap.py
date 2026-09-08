"""Outbox replay: the swap has to hit the row you clicked (§9, §10).

Replay is `hx-swap="outerHTML"` against `#outbox-{{ row.id }}` — a per-row
target, generated per row. Point it at the wrong id and the server still returns
200 with a correct fragment; the only visible symptom is a replayed row
appearing on top of a different one. `test_web.py` asserts the POST flips the
status in the database, which stays true no matter where the HTML lands.
"""

from __future__ import annotations

from selenium.webdriver.common.by import By

from e2e.helpers import enqueue_failed, wait_until

FIRST = {"type": "job", "title": "Software Engineer Intern", "company": "Stripe"}
SECOND = {"type": "job", "title": "Data Science Intern", "company": "Ramp"}


def _status(driver, row_id: int) -> str:
    """The status pill, lowercased — the CSS renders it uppercase.

    `.text` is what the browser paints, not what the template wrote. Asserting
    on typography instead of state is how these tests get brittle.
    """
    return driver.find_element(By.CSS_SELECTOR, f"#outbox-{row_id} .pill").text.lower()


def test_replaying_one_row_swaps_that_row_and_leaves_its_neighbour_alone(driver, live_server):
    older = enqueue_failed(live_server.service, "mk-older", FIRST, error="discord 500")
    newer = enqueue_failed(live_server.service, "mk-newer", SECOND, error="discord 500")

    driver.get(live_server.at("/outbox?status=failed"))
    assert _status(driver, older) == "failed"
    assert _status(driver, newer) == "failed"

    driver.find_element(By.CSS_SELECTOR, f"#outbox-{newer} button").click()

    wait_until(
        driver,
        lambda d: _status(d, newer) == "queued",
        message=f"row {newer} should have been swapped to queued",
    )
    assert _status(driver, older) == "failed", "replaying one row must not redraw another"
    assert live_server.db.scalar("SELECT status FROM outbox WHERE id = ?", (older,)) == "failed"

    # outerHTML replaces the <tr> itself; losing the id would break every
    # subsequent replay on the row while looking fine on screen.
    assert driver.find_elements(By.CSS_SELECTOR, f"#outbox-{newer} button"), (
        "the swapped-in row should still carry its own id and Replay button"
    )
