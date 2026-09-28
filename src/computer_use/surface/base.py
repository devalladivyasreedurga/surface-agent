"""The one interface both the discovery agent and the replay engine depend on.

Neither of those two ever imports Playwright. That is the whole point of this
file: a DesktopSurface (OS accessibility APIs) or LegacyWebSurface (frameset/
table-aware DOM walking) could be dropped in later, and the agent loop,
artifact schema, and replay engine would not change at all -- only the
concrete Locator *values* recorded in an artifact would differ per surface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from computer_use.models.actions import Action, ActionResult
from computer_use.models.surface_state import SurfaceState


class Surface(ABC):
    @abstractmethod
    def observe(self) -> SurfaceState:
        """Return a snapshot of the current state sufficient to decide the next action."""

    @abstractmethod
    def act(self, action: Action) -> ActionResult:
        """Execute one action and report what happened."""

    def close(self) -> None:  # pragma: no cover - optional cleanup hook
        pass
