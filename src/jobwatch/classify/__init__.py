"""Classification without an API (§8): verdict cache, then rules, then a human."""

from .rules import CompiledRule, RuleOutcome, RuleSet
from .verdicts import Classifier, Decision

__all__ = ["Classifier", "CompiledRule", "Decision", "RuleOutcome", "RuleSet"]
