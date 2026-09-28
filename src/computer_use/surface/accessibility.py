"""Flattens Playwright's ARIA snapshot into the flat ElementInfo list SurfaceState uses.

Trade-off (documented per the assignment brief -- "do not overbuild"): we use
Playwright's built-in `locator.aria_snapshot()` rather than hand-rolling a DOM
walker. It is Playwright's supported, actively-maintained accessibility
representation (the older `page.accessibility.snapshot()` API this replaced
is gone as of Playwright 1.4x). It already works on table-based/legacy
layouts with no test ids, which is exactly our target environment, because it
reflects the browser's real accessible-name computation (labels, aria-label,
placeholder-as-name, etc.) rather than raw markup.

Known limitation: we flatten to a list and drop the tree's parent/child
structure. That's a deliberate simplification -- our locator strategy only
ever needs "is there a control with this role and this accessible name",
never "the third cell of the second row of this table". If a future capability
genuinely needs positional/structural targeting, the CSS_TEST_ID or
COORDINATE fallback strategies remain available.
"""
from __future__ import annotations

import re

import yaml

from computer_use.models.surface_state import ElementInfo

_LEAF_RE = re.compile(r'^(?P<role>[\w-]+)(?:\s+"(?P<name>[^"]*)")?(?:\s+\[(?P<attrs>[^\]]*)\])?$')


def _parse_attrs(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
        else:
            out[part] = "true"
    return out


def _parse_leaf(leaf: str) -> ElementInfo | None:
    m = _LEAF_RE.match(leaf.strip())
    if not m:
        return None
    return ElementInfo(
        role=m.group("role"),
        name=m.group("name"),
        attributes=_parse_attrs(m.group("attrs")),
    )


def _walk(node, out: list[ElementInfo]) -> None:
    if node is None:
        return
    if isinstance(node, str):
        el = _parse_leaf(node)
        if el:
            out.append(el)
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("/"):
                # An attribute of the *previous* sibling element (e.g. "/url"
                # under a link), not a new element. Attach to the last element
                # we appended, if any.
                if out:
                    out[-1].attributes[key.lstrip("/")] = str(value)
                continue
            el = _parse_leaf(key)
            if el is None:
                el = ElementInfo(role=key)
            if isinstance(value, str):
                el.text = value
            out.append(el)
            if isinstance(value, (list, dict)):
                _walk(value, out)
        return


def parse_aria_snapshot(raw: str) -> list[ElementInfo]:
    if not raw or not raw.strip():
        return []
    parsed = yaml.safe_load(raw)
    out: list[ElementInfo] = []
    _walk(parsed, out)
    return out
