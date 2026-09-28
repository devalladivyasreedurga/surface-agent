"""Human escalation & handoff.

Core model: automation and the (mocked) operator UI share one in-process
`InterventionManager` and, critically, the *same* `PlaywrightSurface` /
browser window -- there is no second session. Requesting an intervention
blocks the calling thread on a `threading.Event`; the operator UI (a small
FastAPI app running in a background thread, see operator_app.py) is what sets
that event when a human clicks "Resume". This is the seam the assignment
brief asks for: automation can pause, cede control, and something external
can resume it, and there is always exactly one owner of the live session at
a time (the agent loop while running; the human between raise and resume).

Scope cut (documented, not hidden): we do not attempt to capture every
individual mouse/keyboard event the human performs while in control. We
record before/after screenshots and state, and an optional free-text note the
human can leave describing what they did. A full action-level recording of
the human's manual steps is listed as a next step in REPORT.md.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel


class InterventionReason(str, Enum):
    STUCK = "stuck"                                  # discovery agent could not decide a next step
    RISKY_ACTION_CONFIRMATION = "risky_action_confirmation"  # policy engine requires human sign-off
    SAFETY_BLOCK = "safety_block"                     # policy engine hard-blocked an action
    HARD_FAILURE = "hard_failure"                     # replay hit an unrecoverable condition


class InterventionStatus(str, Enum):
    PENDING = "pending"
    RESUMED = "resumed"


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    run_kind: str  # "discovery" | "replay"
    goal_or_capability: str
    current_step: str | None = None
    reason: InterventionReason
    reason_detail: str
    state_url: str
    before_screenshot_ref: str | None = None
    after_screenshot_ref: str | None = None
    created_at: datetime
    resumed_at: datetime | None = None
    status: InterventionStatus = InterventionStatus.PENDING
    operator_note: str | None = None


class InterventionManager:
    """One instance per process, shared between the agent/replay loop (which
    calls raise_and_wait) and the operator FastAPI app (which calls resume)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[str, InterventionRequest] = {}
        self._events: dict[str, threading.Event] = {}

    def create(
        self,
        *,
        run_id: str,
        run_kind: str,
        goal_or_capability: str,
        current_step: str | None,
        reason: InterventionReason,
        reason_detail: str,
        state_url: str,
        before_screenshot_ref: str | None,
    ) -> InterventionRequest:
        req = InterventionRequest(
            id=uuid.uuid4().hex[:12],
            run_id=run_id,
            run_kind=run_kind,
            goal_or_capability=goal_or_capability,
            current_step=current_step,
            reason=reason,
            reason_detail=reason_detail,
            state_url=state_url,
            before_screenshot_ref=before_screenshot_ref,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._requests[req.id] = req
            self._events[req.id] = threading.Event()
        return req

    def wait_for_resume(self, request_id: str, timeout: float | None = None) -> bool:
        event = self._events[request_id]
        return event.wait(timeout=timeout)

    def resume(self, request_id: str, *, operator_note: str | None = None) -> InterventionRequest | None:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status == InterventionStatus.RESUMED:
                return req
            req.status = InterventionStatus.RESUMED
            req.resumed_at = datetime.now(timezone.utc)
            req.operator_note = operator_note
            self._requests[request_id] = req
        self._events[request_id].set()
        return req

    def set_after_screenshot(self, request_id: str, ref: str | None) -> None:
        with self._lock:
            if request_id in self._requests:
                self._requests[request_id].after_screenshot_ref = ref

    def get(self, request_id: str) -> InterventionRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def list_all(self) -> list[InterventionRequest]:
        with self._lock:
            return sorted(self._requests.values(), key=lambda r: r.created_at, reverse=True)

    def list_pending(self) -> list[InterventionRequest]:
        return [r for r in self.list_all() if r.status == InterventionStatus.PENDING]
