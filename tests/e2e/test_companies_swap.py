"""Registry toggles: the swap replaces the card you clicked, and only it (§10).

Both toggles are `hx-swap="outerHTML"` against `closest .company-card`, and the
server answers with that one card. Aim the target anywhere else and the request
still succeeds: the card lands on top of a neighbour, or replaces the whole
page. `test_web.py` sees the fragment, never where it goes.
"""

from __future__ import annotations

from selenium.webdriver.common.by import By

from e2e.helpers import add_company, add_source, wait_until


def _card(driver, name: str):
    return driver.find_element(
        By.XPATH, f"//div[contains(@class,'company-card')][.//h2[normalize-space()='{name}']]"
    )


def _pills(driver, name: str) -> list[str]:
    """Pill text, lowercased: the CSS paints it uppercase."""
    return [p.text.lower() for p in _card(driver, name).find_elements(By.CSS_SELECTOR, ".pill")]


def test_toggles_redraw_the_clicked_card_and_leave_its_neighbour_alone(driver, live_server):
    for slug, name in (("stripe", "Stripe"), ("figma", "Figma")):
        add_company(live_server.db, slug, name=name)
        add_source(live_server.db, slug)

    driver.get(live_server.at("/companies"))
    assert "disabled" not in _pills(driver, "Stripe")

    # The header's button toggles the company; the table's toggles its source.
    _card(driver, "Stripe").find_element(
        By.XPATH, "./div[1]//button[normalize-space()='Disable']"
    ).click()
    wait_until(
        driver,
        lambda d: "disabled" in _pills(d, "Stripe"),
        message="the Stripe card should have been redrawn as disabled",
    )

    _card(driver, "Stripe").find_element(
        By.XPATH, ".//table//button[normalize-space()='Disable']"
    ).click()
    wait_until(
        driver,
        lambda d: "off" in _pills(d, "Stripe"),
        message="the Stripe source should have been redrawn as off",
    )

    assert "disabled" not in _pills(driver, "Figma"), "toggling one card must not redraw another"
    assert len(driver.find_elements(By.CSS_SELECTOR, ".company-card")) == 2
    assert len(driver.find_elements(By.CSS_SELECTOR, "header.masthead")) == 1, (
        "a card swapped into the wrong target can replace the whole page"
    )
