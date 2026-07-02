from dataclasses import dataclass
from enum import Enum


class Classification(str, Enum):
    UNKNOWN = "unknown"
    INTENDED_CHANGE = "intended_change"
    COINCIDENTAL_FIX = "coincidental_fix"
    REGRESSION = "regression"

    def severity_rank(self) -> int:
        """Higher rank = more severe / takes precedence in PR status()."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Classification.UNKNOWN: 0,
    Classification.INTENDED_CHANGE: 1,
    Classification.COINCIDENTAL_FIX: 2,
    Classification.REGRESSION: 3,
}


@dataclass
class ClassificationResult:
    test_code: str
    old_output: str
    new_output: str
    classification: Classification
    classification_explanation: str
