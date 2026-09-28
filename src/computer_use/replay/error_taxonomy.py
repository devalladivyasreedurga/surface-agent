"""Deterministic, pattern-based business-outcome classification.

Documented trade-off: these patterns are specific to the mock bank's copy.
In the real multi-tenant environment (Section 3.7 of the assignment) each
app_key would ship its own small rule set alongside its artifacts -- the
mechanism (regex over the observed page text, no LLM call) stays the same,
only the pattern table changes per app. That is the intended extension
point, not a limitation of the approach.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"No member found with ID \d+"), "MEMBER_NOT_FOUND"),
    (re.compile(r"Member ID must contain only digits"), "VALIDATION_ERROR"),
    (re.compile(r"Initial deposit must be at least \$[\d.]+"), "VALIDATION_ERROR"),
    (re.compile(r"Access denied: this member's record is restricted"), "PERMISSION_DENIED"),
]


def detect_business_outcome(page_text: str) -> tuple[str, str] | None:
    """Returns (code, message) for the first matching pattern, or None."""
    for pattern, code in _PATTERNS:
        m = pattern.search(page_text or "")
        if m:
            return code, m.group(0)
    return None
