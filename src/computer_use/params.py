"""Shared `{{input.x}}` placeholder syntax used by both the artifact recorder
(writes placeholders) and the replay engine (resolves them). Kept in one
place so the two never drift out of sync on the template format.
"""
from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"\{\{input\.([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def make_placeholder(name: str) -> str:
    return f"{{{{input.{name}}}}}"


def resolve(text: str | None, inputs: dict[str, str]) -> str | None:
    if text is None:
        return None

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in inputs:
            raise KeyError(f"missing required input parameter '{key}'")
        return str(inputs[key])

    return _PLACEHOLDER_RE.sub(_sub, text)


def referenced_params(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_PLACEHOLDER_RE.findall(text))
