"""The regex rule engine — stage 2 of classification (§8).

Rules are evaluated against the **original** posting title, not the normalized
one. Normalization sorts tokens and strips years and season words, which would
break patterns like `\\b20\\d\\d\\s+summer\\b` and `\\bsummer analyst\\b`. The
normalized title is only ever used as the verdict-cache key.

Order (binding):
    every location excluded             -> reject   (location-dependent)
    no location satisfies the allow list-> reject   (location-dependent)
    any exclude_any hit                 -> reject
    no require_any hit                  -> reject
    require_any hit AND role_any hit    -> match
    require_any hit, no role_any hit    -> review

Location gating runs FIRST and its outcomes are marked location-dependent,
which makes them uncacheable. The verdict cache is keyed by normalized title
alone, so caching "rejected because it is in London" would reject that same
title in San Francisco forever after. See `Classifier.classify`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..db import Database

__all__ = ["KINDS", "CompiledRule", "RuleOutcome", "RuleSet"]

KINDS = (
    "require_any",
    "role_any",
    "exclude_any",
    "location_exclude",
    "location_require",
)

#: Kinds matched against the posting's locations rather than its title.
LOCATION_KINDS = ("location_exclude", "location_require")


@dataclass(slots=True)
class CompiledRule:
    id: int
    kind: str
    pattern: str
    regex: re.Pattern[str] | None
    note: str | None = None
    error: str | None = None

    @classmethod
    def compile(cls, id: int, kind: str, pattern: str, note: str | None = None) -> CompiledRule:
        try:
            return cls(id, kind, pattern, re.compile(pattern, re.IGNORECASE), note)
        except re.error as exc:
            # A bad pattern typed into the workbench must not break classification
            # for every other posting; it is reported in the UI instead.
            return cls(id, kind, pattern, None, note, error=str(exc))

    def search(self, text: str) -> bool:
        return bool(self.regex and self.regex.search(text))


@dataclass(slots=True)
class RuleOutcome:
    """The verdict plus the evidence, so the review screen can explain itself."""

    classification: str            # 'match' | 'reject' | 'review'
    reason: str
    require_hits: list[str] = field(default_factory=list)
    role_hits: list[str] = field(default_factory=list)
    exclude_hits: list[str] = field(default_factory=list)
    location_hits: list[str] = field(default_factory=list)
    #: Decided by where the job is, not what it is called.
    location_dependent: bool = False

    @property
    def is_decisive(self) -> bool:
        """True when the outcome is safe to cache. `review` never is.

        Neither is anything decided by location: the cache key is the
        normalized title, which says nothing about where the job is.
        """
        return self.classification in ("match", "reject") and not self.location_dependent


class RuleSet:
    """An immutable snapshot of `filter_rules`, compiled once."""

    def __init__(self, rules: list[CompiledRule]) -> None:
        self.all = rules
        self.by_kind: dict[str, list[CompiledRule]] = {k: [] for k in KINDS}
        for rule in rules:
            if rule.kind in self.by_kind:
                self.by_kind[rule.kind].append(rule)

    @classmethod
    def load(cls, db: Database, *, enabled_only: bool = True) -> RuleSet:
        sql = "SELECT id, kind, pattern, note FROM filter_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY kind, id"
        return cls(
            [
                CompiledRule.compile(r["id"], r["kind"], r["pattern"], r["note"])
                for r in db.query(sql)
            ]
        )

    @classmethod
    def from_patterns(cls, patterns: dict[str, list[str]]) -> RuleSet:
        """Build a throwaway set for the workbench's live preview."""
        rules: list[CompiledRule] = []
        for index, kind in enumerate(KINDS):
            for offset, pattern in enumerate(patterns.get(kind, [])):
                rules.append(CompiledRule.compile(-(index * 1000 + offset + 1), kind, pattern))
        return cls(rules)

    @property
    def broken(self) -> list[CompiledRule]:
        return [r for r in self.all if r.error]

    def __len__(self) -> int:
        return len(self.all)

    # -- evaluation --------------------------------------------------------

    def evaluate(self, title: str, locations: list[str] | None = None) -> RuleOutcome:
        rejected = self.evaluate_locations(locations)
        if rejected is not None:
            return rejected
        return self.evaluate_title(title)

    def evaluate_locations(self, locations: list[str] | None = None) -> RuleOutcome | None:
        """Screen on where the job is. Returns a reject, or None to continue.

        A posting with no locations at all is never rejected here: an adapter
        that failed to parse a location must not silently cost you a posting
        that might well have been in range.
        """
        locs = [loc for loc in (locations or []) if loc and loc.strip()]
        if not locs:
            return None

        excluded = self._excluded_locations(locs)
        if len(excluded) == len(locs):
            return RuleOutcome(
                "reject",
                f"every location excluded by {_fmt(sorted(set(excluded)))}",
                location_hits=excluded,
                location_dependent=True,
            )

        allow = self.by_kind["location_require"]
        if allow and not any(rule.search(loc) for loc in locs for rule in allow):
            return RuleOutcome(
                "reject",
                f"no location satisfies {_fmt([r.pattern for r in allow])} — "
                f"{_fmt(locs)} is out of range",
                location_hits=locs,
                location_dependent=True,
            )
        return None

    def evaluate_title(self, title: str) -> RuleOutcome:
        text = title or ""

        excludes = [r.pattern for r in self.by_kind["exclude_any"] if r.search(text)]
        if excludes:
            return RuleOutcome(
                "reject",
                f"excluded by {_fmt(excludes)}",
                exclude_hits=excludes,
            )

        requires = [r.pattern for r in self.by_kind["require_any"] if r.search(text)]
        if not requires:
            return RuleOutcome(
                "reject",
                "no require_any pattern matched — not an internship posting",
            )

        roles = [r.pattern for r in self.by_kind["role_any"] if r.search(text)]
        if roles:
            return RuleOutcome(
                "match",
                f"matched {_fmt(requires)} and {_fmt(roles)}",
                require_hits=requires,
                role_hits=roles,
            )

        return RuleOutcome(
            "review",
            f"matched {_fmt(requires)} but no role_any pattern matched — "
            "it is an internship, but of an unrecognised kind",
            require_hits=requires,
        )

    def _excluded_locations(self, locations: list[str]) -> list[str]:
        hits: list[str] = []
        for loc in locations:
            for rule in self.by_kind["location_exclude"]:
                if rule.search(loc):
                    hits.append(rule.pattern)
                    break
        return hits


def _fmt(patterns: list[str], limit: int = 3) -> str:
    shown = [f"/{p}/" for p in patterns[:limit]]
    if len(patterns) > limit:
        shown.append(f"+{len(patterns) - limit} more")
    return ", ".join(shown) if shown else "nothing"
