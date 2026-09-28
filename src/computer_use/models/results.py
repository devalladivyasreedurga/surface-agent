"""Result contracts returned to callers (an AI agent, in production).

The three-way split below is the single most important design decision in
the replay path: conflating "no such member" with a crash is exactly the
mistake the assignment brief calls out. `RecoveryEvent`s are *not* a fourth
outcome -- they are internal execution events logged along the way; if the
engine recovers, the final status is still success/business_outcome.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"                # capability completed, outputs returned
    BUSINESS_OUTCOME = "business_outcome"  # legitimate domain result, e.g. "member not found"
    HARD_FAILURE = "hard_failure"      # automation could not safely continue


class RecoveryEvent(BaseModel):
    step_id: str
    condition: str          # e.g. "transient_slow_load", "known_interstitial"
    action_taken: str       # e.g. "retried_after_2s", "dismissed_dialog"
    attempt: int


class HardFailureDetail(BaseModel):
    step_id: str
    expected: str
    observed: str
    error_code: str
    message: str
    evidence_ref: str | None = None


class BusinessOutcomeDetail(BaseModel):
    code: str                # e.g. "MEMBER_NOT_FOUND", "VALIDATION_ERROR", "PERMISSION_DENIED"
    message: str
    step_id: str


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    capability_version: int
    run_id: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome: BusinessOutcomeDetail | None = None
    hard_failure: HardFailureDetail | None = None
    recovery_events: list[RecoveryEvent] = Field(default_factory=list)
    steps_completed: int = 0
    steps_total: int = 0


class DiscoveryStopReason(str, Enum):
    SUCCESS = "success"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    DEAD_END = "dead_end"
    SAFETY_BLOCK = "safety_block"
    ESCALATED = "escalated"


class DiscoveryResult(BaseModel):
    run_id: str
    stop_reason: DiscoveryStopReason
    goal: str
    steps_taken: int
    escalation_id: str | None = None

    # What the run actually found -- every value any step extracted during
    # discovery, keyed the same way the eventual artifact's outputs will be,
    # plus the model's own human-readable summary of the outcome. This is
    # run-specific evidence (the literal member/balance from *this* run), not
    # part of the recorded capability -- see ArtifactRecorder, which never
    # sees this field.
    outputs: dict[str, str] = Field(default_factory=dict)
    summary: str | None = None

    # Populated from the model's finish_success call -- the raw, generic
    # capability identity it proposed. Canonicalization/dedup against
    # existing artifacts happens one layer up, in CapabilityRegistry.
    capability_slug: str | None = None
    capability_name: str | None = None
    capability_description: str | None = None
