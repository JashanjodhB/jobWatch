"""Disable failing: asks first, then reloads the page it changed (§10, §12).

The button is a form with `hx-confirm`, and the server answers 204 with an
`HX-Redirect` back to Health. Both halves live in the browser: a dismissed
native dialog must send nothing, and htmx must follow the header rather than
swap an empty body. `test_web.py` sees only the header.
"""

from __future__ import annotations

from selenium.webdriver.common.by import By

from e2e.helpers import add_company, add_source, text_of, wait_until


def _seed(db, slug: str, failures: int) -> int:
    add_company(db, slug)
    sid = add_source(db, slug)
    db.execute("UPDATE sources SET consecutive_failures = ? WHERE id = ?", (failures, sid))
    return sid


def _enabled(db, sid: int) -> int:
    return db.scalar("SELECT enabled FROM sources WHERE id = ?", (sid,))


def _button(driver):
    return driver.find_element(By.XPATH, "//button[starts-with(normalize-space(), 'Disable failing')]")


def test_disable_failing_asks_first_then_reloads_with_what_it_did(driver, live_server):
    db = live_server.db
    failing = _seed(db, "stripe", 6)
    healthy = _seed(db, "figma", 0)
    driver.get(live_server.at("/health"))

    _button(driver).click()
    dialog = wait_until(driver, lambda d: d.switch_to.alert, message="no confirm dialog appeared")
    assert "1 source" in dialog.text, "the prompt should say how many it will switch off"
    dialog.dismiss()
    assert _enabled(db, failing) == 1, "a dismissed confirm must send nothing"

    _button(driver).click()
    wait_until(driver, lambda d: d.switch_to.alert, message="no confirm on the second click").accept()

    wait_until(
        driver,
        lambda d: "disabled=" in d.current_url,
        message="htmx should follow HX-Redirect back to Health",
    )
    assert "Disabled 1 failing source" in text_of(driver, "main")
    assert "stripe/fake" in text_of(driver, "main")
    assert (_enabled(db, failing), _enabled(db, healthy)) == (0, 1)
    assert not driver.find_elements(
        By.XPATH, "//button[starts-with(normalize-space(), 'Disable failing')]"
    ), "nothing is left failing, so the button should be gone"
