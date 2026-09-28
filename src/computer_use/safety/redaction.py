"""Best-effort redaction for anything written to logs/evidence/artifacts.

Scope and limits (documented honestly rather than glossed over):
  - Text/JSON values: pattern- and key-based redaction below.
  - Screenshots: NOT pixel-redacted. We reduce exposure by only capturing
    screenshots on observe/failure/escalation (not routine steps) and by
    keeping /evidence/ out of the repo by default. Real image-level redaction
    (blurring known field regions) is listed as a cut in REPORT.md.
"""
from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "ssn", "social_security_number",
    "card_number", "cvv", "pin",
}

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]+\b", re.IGNORECASE)),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{10,}\b")),
]


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for label, pattern in _PATTERNS:
        out = pattern.sub(f"[REDACTED:{label}]", out)
    return out


def redact_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        if key.lower() in _SENSITIVE_KEYS:
            return "[REDACTED]"
        return redact_text(value)
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [redact_value(key, v) for v in value]
    return value


def redact_dict(d: dict) -> dict:
    return {k: redact_value(k, v) for k, v in d.items()}
