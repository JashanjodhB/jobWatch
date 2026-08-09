"""Tests for the identity core (§5). These gate every alert the system sends."""

from __future__ import annotations

import pytest

from jobwatch.normalize import (
    dedup_key,
    merge_key,
    normalize_locations,
    normalize_title,
)

# The binding case from §5: all three must normalize identically.
SPEC_TRIPLE = [
    "2027 Summer Intern - Software Engineer (Austin, TX)",
    "Summer Intern – Software Engineer",
    "Software Engineer Intern, Summer 2027 [REQ-88213]",
]


def test_spec_triple_normalizes_identically():
    normalized = {normalize_title(t) for t in SPEC_TRIPLE}
    assert len(normalized) == 1, normalized


def test_spec_triple_canonical_form():
    # Word order cannot survive (the third title puts "Intern" last), so the
    # canonical form is the sorted token set.
    assert normalize_title(SPEC_TRIPLE[0]) == "engineer intern software"


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern",
        "Software Engineering Intern",
        "Software Engineering Internship",
        "SOFTWARE ENGINEER INTERN",
        "  Software   Engineer   Intern  ",
        "Software Engineer Intern (Remote)",
        "Software Engineer Intern (San Francisco, CA)",
        "Software Engineer Intern [R-4482991]",
        "Software Engineer Intern #88213",
        "Software Engineer Intern - Summer 2026",
        "Summer 2026 Software Engineer Intern",
        "Intern, Software Engineer",
        "Intern — Software Engineering",
        "Software Engineer Intern, JR0012345",
        "Software Engineer Intern (Multiple Locations)",
    ],
)
def test_common_variants_collapse(title):
    assert normalize_title(title) == "engineer intern software"


def test_years_and_seasons_stripped():
    assert "2027" not in normalize_title("2027 Summer Analyst")
    assert "summer" not in normalize_title("2027 Summer Analyst")
    assert normalize_title("Fall 2026 Data Intern") == normalize_title("Data Intern")


def test_dash_variants_are_equivalent():
    forms = [
        "Intern - Data Engineer",
        "Intern – Data Engineer",
        "Intern — Data Engineer",
        "Intern − Data Engineer",
        "Intern ‐ Data Engineer",
    ]
    assert len({normalize_title(f) for f in forms}) == 1


def test_coop_spelling_variants_collapse():
    forms = ["Software Co-op", "Software Coop", "Software Co op", "Software Co-Ops"]
    assert len({normalize_title(f) for f in forms}) == 1


def test_coop_and_intern_stay_distinct():
    # Different programs at the same company. Merging them would suppress an alert.
    assert normalize_title("Software Engineer Co-op") != normalize_title(
        "Software Engineer Intern"
    )


def test_meaningful_parenthetical_is_kept():
    # Dropping "(Backend)" would merge the backend and frontend reqs into one alert.
    backend = normalize_title("Software Engineer Intern (Backend)")
    frontend = normalize_title("Software Engineer Intern (Frontend)")
    assert backend != frontend
    assert "backend" in backend


def test_distinct_roles_do_not_collide():
    titles = [
        "Software Engineer Intern",
        "Hardware Engineer Intern",
        "Data Scientist Intern",
        "Product Design Intern",
        "Security Engineer Intern",
    ]
    assert len({normalize_title(t) for t in titles}) == len(titles)


def test_empty_and_junk_input():
    assert normalize_title("") == ""
    assert normalize_title("   ") == ""
    assert normalize_title("2027") == ""


def test_normalize_locations_dedups_case_insensitively():
    assert normalize_locations(["Austin, TX", "austin, tx", "Seattle, WA"]) == [
        "Austin, TX",
        "Seattle, WA",
    ]
    assert normalize_locations(None) == []
    assert normalize_locations(["", "  "]) == []


def test_merge_key_ignores_location_and_req_id():
    a = merge_key("stripe", "Software Engineer Intern (Austin, TX) [REQ-1]")
    b = merge_key("stripe", "Software Engineering Internship, Summer 2027")
    assert a == b


def test_merge_key_is_scoped_per_company():
    assert merge_key("stripe", "Software Engineer Intern") != merge_key(
        "figma", "Software Engineer Intern"
    )


def test_dedup_key_prefers_req_id():
    # Same req, wildly different title text -> same row.
    a = dedup_key("stripe", "greenhouse", "4482991", "Software Engineer Intern")
    b = dedup_key("stripe", "greenhouse", "4482991", "Totally Different Title")
    assert a == b


def test_dedup_key_falls_back_to_synthetic_title_key():
    a = dedup_key("stripe", "greenhouse", None, "Software Engineer Intern")
    b = dedup_key("stripe", "greenhouse", None, "Software Engineering Internship")
    assert a == b


def test_dedup_key_is_scoped_per_source():
    a = dedup_key("stripe", "greenhouse", "1", "X")
    b = dedup_key("stripe", "direct.stripe", "1", "X")
    assert a != b


def test_keys_are_stable_length():
    assert len(merge_key("x", "y")) == 32
    assert len(dedup_key("x", "y", "z", "t")) == 32
