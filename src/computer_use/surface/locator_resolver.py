"""Resolves a generic Locator into a live Playwright locator, trying the
ranked strategy chain (primary, then fallbacks) until exactly one visible
element matches.

This is the one place strategy-specific knowledge lives; everything above it
(discovery agent, replay engine, artifact schema) only ever sees the generic
Locator/Target types.
"""
from __future__ import annotations

from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import Page

from computer_use.models.actions import Locator, LocatorStrategy, Target


class UnresolvedTargetError(Exception):
    def __init__(self, target: Target, attempts: list[str]):
        self.target = target
        self.attempts = attempts
        super().__init__(f"No locator strategy resolved a unique element. Tried: {attempts}")


def _split_role_name(value: str) -> tuple[str, str | None]:
    if "|" in value:
        role, name = value.split("|", 1)
        return role.strip(), (name.strip() or None)
    return value.strip(), None


def _unique(candidate: PWLocator) -> PWLocator | None:
    """Returns the locator only if it resolves to exactly one element.

    Deliberately does NOT fall back to `.first` on multiple matches. An
    accessible name can be a substring of an ancestor container's computed
    name (e.g. a page-layout `<td>` whose name is the concatenation of all
    descendant text) -- silently taking the first match in that situation
    means acting on the wrong element with high confidence. Ambiguity is
    treated as "this strategy did not resolve" so the ranked chain can fall
    through to the next strategy, and ultimately to UnresolvedTargetError
    (ambiguous is not resolved, not guessed) rather than mis-clicking.
    """
    try:
        count = candidate.count()
    except Exception:
        return None
    return candidate if count == 1 else None


def _resolve_one(page: Page, loc: Locator) -> PWLocator | None:
    try:
        if loc.strategy == LocatorStrategy.ACCESSIBILITY:
            role, name = _split_role_name(loc.value)
            if not name:
                return _unique(page.get_by_role(role))
            exact = _unique(page.get_by_role(role, name=name, exact=True))
            if exact is not None:
                return exact
            return _unique(page.get_by_role(role, name=name, exact=False))
        elif loc.strategy == LocatorStrategy.SEMANTIC:
            for candidate in (
                page.get_by_label(loc.value, exact=True),
                page.get_by_placeholder(loc.value, exact=True),
                page.get_by_text(loc.value, exact=True),
                page.get_by_label(loc.value, exact=False),
                page.get_by_placeholder(loc.value, exact=False),
                page.get_by_text(loc.value, exact=False),
            ):
                resolved = _unique(candidate)
                if resolved is not None:
                    return resolved
            return None
        elif loc.strategy == LocatorStrategy.CSS_TEST_ID:
            return _unique(page.locator(loc.value))
        else:  # COORDINATE handled separately by the caller, not resolvable to a PW locator
            return None
    except Exception:
        return None


def resolve_target(page: Page, target: Target) -> tuple[PWLocator | None, Locator]:
    """Returns (playwright_locator_or_None, the Locator entry that matched/was attempted last).

    A None playwright_locator with strategy COORDINATE means: caller should
    fall back to raw mouse coordinates from `loc.value` ("x,y").
    """
    attempts: list[str] = []
    for loc in target.ranked():
        if loc.strategy == LocatorStrategy.COORDINATE:
            return None, loc  # last resort, always handled by caller via raw coordinates
        resolved = _resolve_one(page, loc)
        attempts.append(f"{loc.strategy.value}:{loc.value}")
        if resolved is not None:
            return resolved, loc
    raise UnresolvedTargetError(target, attempts)
