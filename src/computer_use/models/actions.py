"""Generic, surface-agnostic action vocabulary.

The discovery agent and replay engine only ever speak in these types. Nothing
here is Playwright-shaped -- a DesktopSurface or LegacyWebSurface would consume
the exact same Action/ActionResult contract.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    EXTRACT = "extract"
    WAIT = "wait"


class LocatorStrategy(str, Enum):
    """Ranked roughly by how well a strategy survives a legacy, no-test-id UI.

    ACCESSIBILITY (role + accessible name) and SEMANTIC (label/text) are
    DOM-shape-agnostic and are what a human operator effectively uses, so they
    are preferred. CSS_TEST_ID is promoted above them only when the recorder
    actually finds a stable id/data-testid attribute. COORDINATE is a last
    resort and is always tagged low-confidence -- it is never treated as
    equivalent to a semantic locator by the replay engine.
    """

    ACCESSIBILITY = "accessibility"       # role + accessible name
    SEMANTIC = "semantic"                 # label / visible text
    CSS_TEST_ID = "css_test_id"           # stable id / data-testid / css
    COORDINATE = "coordinate"             # last-resort, low-confidence


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str = Field(..., description="Role+name string, text, CSS selector, or 'x,y' coordinate pair.")
    confidence: Literal["high", "medium", "low"] = "high"
    note: str | None = Field(
        default=None, description="Why this strategy/value was chosen, for human review."
    )

    def model_post_init(self, __context: Any) -> None:
        if self.strategy == LocatorStrategy.COORDINATE and self.confidence != "low":
            # Coordinate targeting must never silently masquerade as a robust locator.
            object.__setattr__(self, "confidence", "low")


class Target(BaseModel):
    """What an action acts on: a primary locator plus ranked fallbacks."""

    primary: Locator
    fallbacks: list[Locator] = Field(default_factory=list)

    def ranked(self) -> list[Locator]:
        return [self.primary, *self.fallbacks]


class Action(BaseModel):
    """One step's worth of intent. Produced by the LLM during discovery,
    consumed verbatim (with parameters resolved) during replay."""

    type: ActionType
    target: Target | None = Field(
        default=None, description="Required for click/fill/extract. Not used for navigate/wait."
    )
    value: str | None = Field(
        default=None,
        description="URL for navigate, text to type for fill, extraction key for extract.",
    )
    timeout_ms: int = 15_000
    reasoning: str | None = Field(
        default=None, description="LLM's stated reason for this action (discovery only, for evidence)."
    )


class ActionOutcome(str, Enum):
    OK = "ok"
    RECOVERED = "recovered"       # e.g. dismissed an interstitial, retried a slow load
    BLOCKED = "blocked"           # policy engine refused to execute
    FAILED = "failed"


class ActionResult(BaseModel):
    outcome: ActionOutcome
    detail: str | None = None
    extracted: dict[str, str] | None = None
    recovery_note: str | None = None
