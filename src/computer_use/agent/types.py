"""Types shared between the LLM client and the discovery agent loop."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from computer_use.models.actions import Action, ActionResult
from computer_use.models.surface_state import SurfaceState


class DecisionKind(str, Enum):
    ACTION = "action"
    FINISH_SUCCESS = "finish_success"
    FINISH_STUCK = "finish_stuck"


class AgentDecision(BaseModel):
    kind: DecisionKind
    action: Action | None = None
    reasoning: str = ""
    summary: str | None = None  # set on finish_success, human-readable run summary (evidence only)

    # Set on finish_success. The model is asked to name/describe the *reusable
    # capability*, abstracted away from this specific run's inputs -- this is
    # what lets ArtifactRecorder produce a canonical, dedupable identity
    # instead of one baked from the literal goal text. See capability_registry.py.
    capability_slug: str | None = None
    capability_name: str | None = None
    capability_description: str | None = None


class StepTrace(BaseModel):
    """One executed step of a discovery run -- the raw material the artifact
    recorder turns into a CapabilityArtifact's steps."""

    step_index: int
    state_before: SurfaceState
    action: Action
    result: ActionResult

