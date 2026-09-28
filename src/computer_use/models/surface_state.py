"""What the agent perceives at each step.

Deliberately thin: enough for an LLM (or a human reviewer) to decide the next
action, not a full DOM dump. A real accessibility tree easily runs to
thousands of nodes on an enterprise app; we prune to interactive/labelled
elements only. Documented trade-off: this can miss a control with no
accessible name and no visible text, which is the same failure mode a
screen-reader user would hit -- at that point the model falls back to
screenshot + coordinate targeting (low confidence, see Locator).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ElementInfo(BaseModel):
    role: str | None = None
    name: str | None = None
    text: str | None = None
    tag: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    bbox: tuple[float, float, float, float] | None = None  # x, y, w, h -- for coordinate fallback only


class SurfaceState(BaseModel):
    url: str
    title: str
    elements: list[ElementInfo] = Field(default_factory=list)
    visible_text_excerpt: str | None = Field(
        default=None, description="Short text summary of the page, capped, for LLM context."
    )
    screenshot_ref: str | None = Field(
        default=None, description="Path/id of a saved screenshot, if one was captured for this observation."
    )
