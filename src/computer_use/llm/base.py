"""Provider-agnostic contract for the discovery agent's LLM.

This is the whole point of the abstraction: swap Anthropic for another
provider by writing one new class, without touching DiscoveryAgent. Replay
never imports anything from this package -- that import boundary is what
makes "zero LLM calls during replay" structurally true rather than a
convention someone can forget.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from computer_use.agent.types import AgentDecision
from computer_use.models.surface_state import SurfaceState


class LLMClient(ABC):
    @abstractmethod
    def decide(self, *, goal: str, state: SurfaceState, history: list[str]) -> AgentDecision:
        """Given the goal, the current observed state, and a short trace of
        prior (action -> outcome) history, return the next thing to do."""
