"""The three-stage classifier (§8). No network calls, no cost.

    1. verdict cache   -- `title_verdicts` keyed by normalized title
    2. rules           -- the regex engine, which caches its decisive outcomes
    3. human review    -- alert anyway, and queue it for a one-key verdict

Stage 1 runs first so a human decision made once is never revisited and always
overrides the rules.

The subtlety: rules also write cache entries, which would freeze the filter
workbench solid — edit a pattern, and every title already cached keeps its old
verdict. So editing rules purges every `source='rules'` row. Manual verdicts are
never purged; that is the whole point of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import Database, utcnow
from ..normalize import normalize_title
from .rules import RuleOutcome, RuleSet

__all__ = ["Classifier", "Decision"]


@dataclass(slots=True)
class Decision:
    classification: str          # 'match' | 'reject' | 'review'
    class_source: str            # 'rules' | 'manual' | 'seed'
    category: str | None = None
    reason: str = ""
    normalized_title: str = ""
    outcome: RuleOutcome | None = None

    @property
    def alertable(self) -> bool:
        return self.classification in ("match", "review")


class Classifier:
    """Holds a compiled RuleSet and the verdict cache. Cheap to call in a loop."""

    def __init__(self, db: Database, *, rules: RuleSet | None = None) -> None:
        self.db = db
        self._rules = rules or RuleSet.load(db)

    @property
    def rules(self) -> RuleSet:
        return self._rules

    def reload_rules(self) -> RuleSet:
        self._rules = RuleSet.load(self.db)
        return self._rules

    # -- classification ----------------------------------------------------

    def classify(
        self,
        title: str,
        locations: list[str] | None = None,
        company_slug: str | None = None,
        *,
        cache: bool = True,
    ) -> Decision:
        normalized = normalize_title(title)

        cached = self.lookup(normalized)
        if cached is not None:
            return Decision(
                classification=cached["verdict"],
                class_source=cached["source"],
                category=cached["category"],
                reason=(
                    "human verdict recorded "
                    if cached["source"] == "manual"
                    else "cached rule outcome from "
                )
                + cached["decided_at"],
                normalized_title=normalized,
            )

        outcome = self._rules.evaluate(title, locations)
        decision = Decision(
            classification=outcome.classification,
            class_source="rules",
            category=None,
            reason=outcome.reason,
            normalized_title=normalized,
            outcome=outcome,
        )

        if cache and outcome.is_decisive and normalized:
            self.record(
                normalized,
                outcome.classification,
                source="rules",
                sample_title=title,
                sample_company=company_slug,
            )
        return decision

    def explain(self, title: str, locations: list[str] | None = None) -> Decision:
        """Classify without touching the cache. Backs `jobwatch classify` and the UI."""
        return self.classify(title, locations, cache=False)

    # -- cache -------------------------------------------------------------

    def lookup(self, normalized_title: str):
        if not normalized_title:
            return None
        return self.db.one(
            "SELECT normalized_title, verdict, category, source, decided_at "
            "FROM title_verdicts WHERE normalized_title = ?",
            (normalized_title,),
        )

    def record(
        self,
        normalized_title: str,
        verdict: str,
        *,
        source: str = "manual",
        category: str | None = None,
        sample_title: str | None = None,
        sample_company: str | None = None,
    ) -> None:
        """Write a verdict. A manual verdict always wins over a cached rule one."""
        if not normalized_title:
            return
        if verdict not in ("match", "reject"):
            raise ValueError(f"verdict must be match or reject, got {verdict!r}")

        self.db.execute(
            """
            INSERT INTO title_verdicts(
                normalized_title, verdict, category, source, decided_at,
                sample_title, sample_company)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(normalized_title) DO UPDATE SET
                verdict       = excluded.verdict,
                category      = excluded.category,
                source        = excluded.source,
                decided_at    = excluded.decided_at,
                sample_title  = COALESCE(title_verdicts.sample_title, excluded.sample_title),
                sample_company= COALESCE(title_verdicts.sample_company, excluded.sample_company)
            WHERE excluded.source = 'manual' OR title_verdicts.source != 'manual'
            """,
            (
                normalized_title,
                verdict,
                category,
                source,
                utcnow(),
                sample_title,
                sample_company,
            ),
        )

    def forget(self, normalized_title: str) -> None:
        """Undo a verdict. Backs the `U` key in the review queue."""
        self.db.execute(
            "DELETE FROM title_verdicts WHERE normalized_title = ?", (normalized_title,)
        )

    def purge_rule_verdicts(self) -> int:
        """Drop every rules-derived cache entry, keeping human decisions.

        Called whenever `filter_rules` changes. Without it, a workbench edit
        would have no effect on any title the rules had already seen.
        """
        cur = self.db.execute("DELETE FROM title_verdicts WHERE source = 'rules'")
        self.reload_rules()
        return cur.rowcount or 0

    # -- bulk application --------------------------------------------------

    def apply_verdict_to_jobs(self, normalized_title: str, verdict: str, category: str | None) -> int:
        """Re-stamp existing rows after a human verdict, so the feed reflects it."""
        cur = self.db.execute(
            "UPDATE jobs SET classification = ?, class_source = 'manual', category = ? "
            "WHERE normalized_title = ? AND classification != ?",
            (verdict, category, normalized_title, verdict),
        )
        return cur.rowcount or 0
