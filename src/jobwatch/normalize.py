"""Title normalization and the two identity keys (§5).

This module is the correctness core of the system. `merge_key` decides whether
a posting produces a Discord alert, so a bug here is either a missed internship
or a duplicate storm. Tests come first; see tests/test_normalize.py.

Why titles are token-sorted
---------------------------
The spec requires these three to normalize *identically*:

    "2027 Summer Intern - Software Engineer (Austin, TX)"
    "Summer Intern - Software Engineer"
    "Software Engineer Intern, Summer 2027 [REQ-88213]"

The third puts "Intern" last while the others put it first, so word order
cannot be preserved. The canonical form is therefore the deduplicated, sorted
token set: all three collapse to "engineer intern software".

That also means classification rules must NOT run against normalized titles --
sorting and season-stripping would break patterns like '\\b20\\d\\d\\s+summer\\b'.
Rules match the original title; `title_verdicts` is keyed by the normalized one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

__all__ = [
    "dedup_key",
    "merge_key",
    "normalize_locations",
    "normalize_title",
]

# ── character-level cleanup ───────────────────────────────────────────────

# Every dash variant an ATS has ever emitted, folded to ASCII hyphen.
_DASH_TRANS = {ord(c): "-" for c in "‐‑‒–—―−˗֊⁃－"}
_QUOTE_TRANS = {ord("’"): "'", ord("‘"): "'", ord("“"): '"', ord("”"): '"'}

_YEAR = re.compile(r"\b(?:fy\s?)?(?:19|20)\d{2}\b", re.I)
_SHORT_FY = re.compile(r"\bfy\s?\d{2}\b", re.I)
_SEASON = re.compile(r"\b(?:summer|fall|autumn|winter|spring)\b", re.I)

# Requisition identifiers. Ordered widest-first; all run.
_REQ_PATTERNS = [
    re.compile(r"\[[^\]]*\d[^\]]*\]"),                      # [REQ-88213]
    re.compile(r"\b(?:req|requisition|job|jr|posting)\b[\s#:_-]*[a-z]?\d[\w-]*", re.I),
    re.compile(r"\b[a-z]{1,3}[-_]?\d{4,}\b", re.I),         # R-12345, JR0012345
    re.compile(r"#\s*\d+"),                                 # #98213
    re.compile(r"\b\d{4,}\b"),                              # bare long digit runs
]

# Hyphenated compounds folded before hyphens become separators, so "co-op" and
# "coop" cannot produce different merge keys.
_COMPOUNDS = [
    (re.compile(r"\bco[\s-]?ops?\b", re.I), "coop"),
    (re.compile(r"\bfull[\s-]?stack\b", re.I), "fullstack"),
    (re.compile(r"\bfront[\s-]?end\b", re.I), "frontend"),
    (re.compile(r"\bback[\s-]?end\b", re.I), "backend"),
    (re.compile(r"\be[\s-]?commerce\b", re.I), "ecommerce"),
    (re.compile(r"\bon[\s-]?site\b", re.I), "onsite"),
    (re.compile(r"\bmachine[\s-]?learning\b", re.I), "machinelearning"),
    (re.compile(r"\bnew[\s-]?grad\b", re.I), "newgrad"),
]

_NON_WORD = re.compile(r"[^a-z0-9]+")

# Words that carry no identity. Deliberately short -- every entry here is a
# potential over-merge.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "for", "and", "or", "to", "in", "at", "with",
        "our", "on", "by", "program", "programme", "opportunity", "opportunities",
        "position", "positions", "role", "roles", "job", "jobs", "opening",
        "openings", "hiring", "student", "students", "university", "undergrad",
        "undergraduate", "level", "remote", "hybrid", "onsite", "various",
        "multiple", "locations", "location",
    }
)

# Spelling variants that genuinely denote the same posting.
_SYNONYMS = {
    "internship": "intern",
    "interns": "intern",
    "internships": "intern",
    "engineering": "engineer",
    "engineers": "engineer",
    "developers": "developer",
    "dev": "developer",
    "sciences": "science",
    "scientists": "scientist",
    "analysts": "analyst",
    "coops": "coop",
    "apprenticeship": "apprentice",
    "apprentices": "apprentice",
    "ml": "machinelearning",
    "swe": "software",
    "sde": "software",
}

# ── trailing fragment stripping ───────────────────────────────────────────

_TRAILING_GROUP = re.compile(r"\s*[\(\[\{]([^\)\]\}]*)[\)\]\}]\s*$")

# A trailing parenthetical is stripped unless it names a *role* qualifier.
#
# Deciding this the other way round -- listing what counts as a place -- does not
# work: "(Austin, TX)" is recognisable, but "(NYC)", "(London)" and "(Bangalore)"
# are just capitalised words, and no list of cities stays complete. Role
# qualifiers are a small, closed, stable vocabulary, so that is what gets
# enumerated.
#
# The failure modes are asymmetric and this picks the cheaper one. Stripping a
# role qualifier merges the backend and frontend reqs into one alert; keeping a
# city splits one role across six cities into six alerts. §5 is explicit that
# one role in six cities is one alert.
_ROLE_QUALIFIER = re.compile(
    r"\b("
    r"back[\s-]?end|front[\s-]?end|full[\s-]?stack|"
    r"ios|android|mobile|web|embedded|firmware|hardware|silicon|"
    r"machine\s*learning|deep\s*learning|ml|ai|nlp|vision|robotics|"
    r"data|analytics|database|storage|"
    r"infra(structure)?|platform|systems|cloud|network(ing)?|distributed|"
    r"security|privacy|crypto|compiler|graphics|kernel|"
    r"devops|sre|reliability|qa|test(ing)?|automation|"
    r"research|quant(itative)?|trading|algorithms?|"
    r"design|ux|ui|product|"
    r"software|engineering|science"
    r")\b",
    re.I,
)


def _is_role_qualifier(fragment: str) -> bool:
    """True if a trailing parenthetical narrows the *role*, not the place or date."""
    frag = fragment.strip()
    if not frag or any(ch.isdigit() for ch in frag):
        return False
    if "," in frag:  # a comma in a trailing group is nearly always a place list
        return False
    return bool(_ROLE_QUALIFIER.search(frag))


def _strip_trailing_fragments(text: str) -> str:
    """Remove trailing (...) / [...] groups that are locations, dates, or req IDs."""
    while True:
        m = _TRAILING_GROUP.search(text)
        if not m:
            return text
        inner = m.group(1)
        if not _is_role_qualifier(inner):
            text = text[: m.start()]
            continue
        # Meaningful qualifier: keep the words, drop the brackets.
        return f"{text[: m.start()]} {inner}"


# ── public API ────────────────────────────────────────────────────────────


def normalize_title(title: str) -> str:
    """Fold a posting title to its canonical identity form.

    Case-insensitively: strips years, season words, requisition numbers and
    trailing location parentheticals; folds dash variants and hyphenated
    compounds; drops stopwords; applies synonyms; then returns the sorted,
    deduplicated token set joined by spaces.
    """
    if not title:
        return ""

    s = unicodedata.normalize("NFKC", title)
    s = s.translate(_DASH_TRANS).translate(_QUOTE_TRANS)

    s = _strip_trailing_fragments(s)

    for pat in _REQ_PATTERNS:
        s = pat.sub(" ", s)

    for pat, repl in _COMPOUNDS:
        s = pat.sub(repl, s)

    s = _SHORT_FY.sub(" ", s)
    s = _YEAR.sub(" ", s)
    s = _SEASON.sub(" ", s)

    s = _NON_WORD.sub(" ", s.lower())

    tokens: set[str] = set()
    for tok in s.split():
        tok = _SYNONYMS.get(tok, tok)
        if tok and tok not in _STOPWORDS and not tok.isdigit():
            tokens.add(tok)

    return " ".join(sorted(tokens))


def normalize_locations(locations: list[str] | None) -> list[str]:
    """Deduplicate and tidy location strings for storage and display.

    Order is preserved -- the first location an adapter reports is usually the
    primary one, and the feed shows it first.
    """
    if not locations:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for loc in locations:
        if not loc:
            continue
        cleaned = unicodedata.normalize("NFKC", str(loc)).translate(_DASH_TRANS)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;|-")
        if not cleaned:
            continue
        fold = cleaned.casefold()
        if fold not in seen:
            seen.add(fold)
            out.append(cleaned)
    return out


def _sha(basis: str) -> str:
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def dedup_key(company_slug: str, source_name: str, req_id: str | None, title: str) -> str:
    """Identity WITHIN a source. Prevents re-processing the same row each poll."""
    if req_id:
        basis = f"{company_slug}|{source_name}|req|{req_id}"
    else:
        basis = f"{company_slug}|{source_name}|syn|{normalize_title(title)}"
    return _sha(basis)


def merge_key(company_slug: str, title: str) -> str:
    """Identity ACROSS sources. Gates alerting.

    Deliberately excludes location and requisition ID: one role posted in six
    cities and surfaced by two sources is one alert.
    """
    return _sha(f"{company_slug}|{normalize_title(title)}")
