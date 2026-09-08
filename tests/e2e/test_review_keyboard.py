"""The review queue as a person actually uses it: keyboard only (§10).

`test_web.py` proves `POST /review/{key}/verdict` records a verdict and returns
the next card. Everything between a keystroke and that request lives in
`static/app.js` and has never had a test: the keydown handler, the hidden field
`selectCategory` writes into, the modifier guard, and the HTMX swap that has to
put the response inside `#review-card` and nowhere else.
"""

from __future__ import annotations

from selenium.webdriver.common.by import By

from e2e.helpers import card_title, press, seed_review_queue, verdicts, wait_for_card

TITLES = [
    "Software Engineer Intern Platform",
    "Data Science Intern Analytics",
    "Hardware Validation Intern Silicon",
]


def _open_review(driver, live_server) -> None:
    seed_review_queue(live_server.db, TITLES)
    driver.get(live_server.at("/review"))
    wait_for_card(driver, TITLES[0])


def test_m_records_a_match_and_swaps_the_next_card_into_place(driver, live_server):
    """The whole keyboard path, end to end: keydown -> POST -> swap."""
    _open_review(driver, live_server)

    press(driver, "m")

    # The decided card leaves the queue, so the same offset lands on the next one.
    wait_for_card(driver, TITLES[1])
    recorded = verdicts(live_server.db)
    assert len(recorded) == 1, f"expected exactly one verdict, got {recorded}"
    assert next(iter(recorded.values()))[0] == "match"


def test_a_number_key_tags_the_category_that_the_verdict_then_stores(driver, live_server):
    """`2` has to reach the database, and only JavaScript carries it there.

    `selectCategory` writes the chosen category into a hidden input that the
    verdict form submits. Nothing server-side can tell a correct binding from
    one that tags every posting `swe`: a request-level test supplies the field
    itself, so it would agree with any wiring at all.
    """
    _open_review(driver, live_server)

    press(driver, "2")

    buttons = driver.find_elements(By.CSS_SELECTOR, "#review-card [data-category]")
    chosen = buttons[1].get_attribute("data-category")
    assert chosen == "ai-ml", "the 1-4 keys index the category row in order"
    assert buttons[1].get_attribute("aria-pressed") == "true"
    assert [b.get_attribute("aria-pressed") for b in buttons].count("true") == 1
    assert (
        driver.find_element(By.CSS_SELECTOR, "#review-card input[name='category']").get_attribute(
            "value"
        )
        == chosen
    )

    press(driver, "m")
    wait_for_card(driver, TITLES[1])

    recorded = verdicts(live_server.db)
    assert list(recorded.values()) == [("match", "ai-ml")]


def test_j_and_k_walk_the_queue_without_deciding_anything(driver, live_server):
    """Skip and back are `hx-get`s against an offset; neither may write."""
    _open_review(driver, live_server)

    press(driver, "j")
    wait_for_card(driver, TITLES[1])
    press(driver, "j")
    wait_for_card(driver, TITLES[2])
    press(driver, "k")
    wait_for_card(driver, TITLES[1])

    assert verdicts(live_server.db) == {}, "navigating is not deciding"


def test_undo_reaches_back_through_the_swap_that_created_it(driver, live_server):
    """The Undo button only exists in the fragment the verdict swapped in.

    So this fails unless the swapped-in HTML is live: HTMX has to have processed
    the new node's own `hx-post`, and `app.js` has to find its `[data-act='undo']`
    inside a card it never saw at page load.
    """
    _open_review(driver, live_server)

    press(driver, "m")
    wait_for_card(driver, TITLES[1])
    assert driver.find_elements(By.CSS_SELECTOR, "#review-card [data-act='undo']")

    press(driver, "u")

    # The undone title returns to the queue, and it was the most recently seen,
    # so it comes back at the front.
    wait_for_card(driver, TITLES[0])
    assert verdicts(live_server.db) == {}


def test_a_modified_keypress_is_left_to_the_browser(driver, live_server):
    """Ctrl+R must reload the page, not permanently reject the job on screen.

    A verdict is global and forever, so a shortcut that fires under a modifier
    is not a cosmetic bug — it silently poisons the classifier for every future
    posting with that title. Ctrl+M stands in for the class here because
    Ctrl+R would take the page with it.
    """
    _open_review(driver, live_server)

    press(driver, "m", ctrl=True)
    assert card_title(driver) == TITLES[0]

    # Proving a negative needs a barrier, not a sleep. An unmodified 'j' makes
    # its own round trip; once its swap has landed, any request Ctrl+M might
    # have started has had at least as long to land too.
    press(driver, "j")
    wait_for_card(driver, TITLES[1])

    assert verdicts(live_server.db) == {}, "a modified keypress must not record a verdict"
