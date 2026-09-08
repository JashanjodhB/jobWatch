"""The filter workbench: debounced live preview, and the confirm on delete (§10).

Two behaviours here have no server-side shadow at all. The preview fires from
`hx-trigger="keyup changed delay:350ms"`, so nothing requests it unless a real
keystroke lands and the debounce elapses. And `hx-confirm` is `window.confirm` —
a native dialog, which is the one thing an HTTP-level test can never stand in
front of. `test_web.py` can only check that `POST /filters/{id}/delete` deletes,
which is exactly the half that was never in question.
"""

from __future__ import annotations

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

from e2e.helpers import (
    add_filter_rule,
    seed_review_queue,
    text_of,
    wait_for_selector,
    wait_for_text,
    wait_until,
)

TITLES = [
    "Machine Learning Intern Perception",
    "Software Engineer Intern Platform",
    "Hardware Validation Intern Silicon",
]

PLACEHOLDER = "Type a pattern to see exactly which postings would flip"


def _rule_line(driver, pattern: str):
    """The rule row whose pattern is exactly `pattern`, or None.

    Tolerates a row going stale mid-scan: `#rule-list` is replaced wholesale by
    every save, toggle and delete, so a scan can overlap a swap.
    """
    for line in driver.find_elements(By.CSS_SELECTOR, "#rule-list .rule-line"):
        try:
            cells = line.find_elements(By.CSS_SELECTOR, "span.pattern")
            if cells and cells[0].text == pattern:
                return line
        except StaleElementReferenceException:
            continue
    return None


def test_the_preview_panel_answers_what_you_typed(driver, live_server):
    """A debounced keyup has to carry the *current* input value to the server.

    The count is the assertion that matters: `machine learning` hits exactly one
    of the three seeded titles, so a preview built from a stale or empty value
    reports a different number rather than merely looking wrong.
    """
    seed_review_queue(live_server.db, TITLES)
    driver.get(live_server.at("/filters"))
    assert PLACEHOLDER in text_of(driver, "#preview")

    driver.find_element(By.CSS_SELECTOR, "input[name='pattern']").send_keys("machine learning")

    wait_for_selector(driver, "#preview .stat-row")
    hits = driver.find_element(By.CSS_SELECTOR, "#preview .stat .n").text
    assert hits == "1", f"'machine learning' matches one of {len(TITLES)} seeded titles, got {hits}"


def test_an_uncompilable_pattern_is_reported_before_it_can_be_saved(driver, live_server):
    """The preview is also the regex error channel, and it is live-typed too."""
    seed_review_queue(live_server.db, TITLES)
    driver.get(live_server.at("/filters"))

    driver.find_element(By.CSS_SELECTOR, "input[name='pattern']").send_keys("intern(")

    wait_for_text(driver, "#preview", "invalid regex")
    assert "will not be saved" in text_of(driver, "#preview")


def test_deleting_a_rule_asks_first_and_a_dismissed_dialog_deletes_nothing(driver, live_server):
    """`hx-confirm` is a native dialog: dismissing it must abort the request.

    Delete is the one destructive control in the UI and it has no undo, so
    "the confirm is wired up" is worth more than it looks. Nothing below the
    browser can observe it.
    """
    pattern = r"\bzz-e2e-marker\b"
    rule_id = add_filter_rule(live_server.db, "exclude_any", pattern, note="e2e fixture")
    driver.get(live_server.at("/filters"))

    line = _rule_line(driver, pattern)
    assert line is not None, "the seeded rule should be listed"
    line.find_element(By.XPATH, ".//button[normalize-space()='Delete']").click()

    dialog = wait_until(driver, lambda d: d.switch_to.alert, message="no confirm dialog appeared")
    assert pattern in dialog.text, "the prompt should name the rule it is about to destroy"
    dialog.dismiss()

    assert live_server.db.one("SELECT id FROM filter_rules WHERE id = ?", (rule_id,)) is not None
    assert _rule_line(driver, pattern) is not None, "a dismissed confirm leaves the row alone"

    # And accepting goes through, so the dismissal above proves the guard rather
    # than a broken button.
    _rule_line(driver, pattern).find_element(
        By.XPATH, ".//button[normalize-space()='Delete']"
    ).click()
    wait_until(driver, lambda d: d.switch_to.alert, message="no confirm on the second click").accept()

    wait_until(
        driver,
        lambda d: _rule_line(d, pattern) is None,
        message="the row should disappear once the confirm is accepted",
    )
    assert live_server.db.one("SELECT id FROM filter_rules WHERE id = ?", (rule_id,)) is None
