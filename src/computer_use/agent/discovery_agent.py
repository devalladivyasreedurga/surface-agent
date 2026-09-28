"""The observe -> decide -> act loop.

observe (Surface) -> decide (LLMClient) -> validate (PolicyEngine) -> act
(Surface) -> observe again. Stops on success, max steps, timeout, dead end,
a safety block, or an escalation the human does not resolve in time.

This module imports both `llm` and `surface` -- replay.py deliberately does
not import `llm` at all, which is what makes "replay never calls the model"
a structural fact rather than a runtime setting someone could flip.
"""
from __future__ import annotations

import time

from computer_use.agent.types import AgentDecision, DecisionKind, StepTrace
from computer_use.evidence.logger import RunLogger
from computer_use.escalation.intervention import InterventionManager, InterventionReason
from computer_use.llm.base import LLMClient
from computer_use.models.actions import ActionOutcome
from computer_use.models.actions import Action, ActionType
from computer_use.models.artifact import RiskLevel
from computer_use.models.results import DiscoveryResult, DiscoveryStopReason
from computer_use.models.surface_state import SurfaceState
from computer_use.models.target_spec import TargetSpec
from computer_use.safety.policy import PolicyEngine
from computer_use.surface.base import Surface

_MAX_CONSECUTIVE_FAILURES = 3


class DiscoveryAgent:
    def __init__(
        self,
        *,
        llm: LLMClient,
        surface: Surface,
        policy: PolicyEngine,
        interventions: InterventionManager,
        logger: RunLogger,
        operator_base_url: str,
        max_steps: int = 20,
        timeout_seconds: float = 300.0,
    ):
        self._llm = llm
        self._surface = surface
        self._policy = policy
        self._interventions = interventions
        self._logger = logger
        self._operator_base_url = operator_base_url
        self._max_steps = max_steps
        self._timeout_seconds = timeout_seconds

    def run(self, *, goal: str, target: TargetSpec) -> tuple[DiscoveryResult, list[StepTrace], SurfaceState | None]:
        run_id = self._logger.run_id
        self._logger.log("discovery_started", run_id=run_id, goal=goal, target=target.model_dump())

        self._logger.log("initial_navigation", url=target.url)
        self._surface.act(Action(type=ActionType.NAVIGATE, value=target.url, reasoning="initial navigation"))

        history: list[str] = []
        traces: list[StepTrace] = []
        consecutive_failures = 0
        start = time.time()
        final_state: SurfaceState | None = None
        stop_reason = DiscoveryStopReason.MAX_STEPS
        escalation_id: str | None = None
        capability_slug: str | None = None
        capability_name: str | None = None
        capability_description: str | None = None
        summary: str | None = None

        for step_index in range(self._max_steps):
            if time.time() - start > self._timeout_seconds:
                stop_reason = DiscoveryStopReason.TIMEOUT
                break

            state = self._surface.observe()
            decision = self._llm.decide(goal=goal, state=state, history=history)
            self._logger.log(
                "llm_decision", step=step_index, kind=decision.kind.value, reasoning=decision.reasoning
            )

            if decision.kind == DecisionKind.FINISH_SUCCESS:
                final_state = self._surface.observe()
                stop_reason = DiscoveryStopReason.SUCCESS
                summary = decision.summary
                capability_slug = decision.capability_slug
                capability_name = decision.capability_name
                capability_description = decision.capability_description
                self._logger.log(
                    "discovery_finished",
                    reason="success",
                    summary=decision.summary,
                    capability_slug=capability_slug,
                    capability_name=capability_name,
                )
                break

            if decision.kind == DecisionKind.FINISH_STUCK:
                escalation_id = self._escalate(
                    run_id=run_id,
                    goal=goal,
                    current_step=f"step_{step_index}",
                    reason=InterventionReason.STUCK,
                    reason_detail=decision.reasoning,
                    state=state,
                )
                history.append(f"[human intervened after agent reported stuck: {decision.reasoning}]")
                continue

            action = decision.action
            assert action is not None

            policy_decision = self._policy.evaluate(action, current_url=state.url, step_risk=RiskLevel.SAFE)
            if not policy_decision.allowed:
                self._logger.log("policy_blocked", step=step_index, reason=policy_decision.reason)
                stop_reason = DiscoveryStopReason.SAFETY_BLOCK
                break

            if policy_decision.requires_confirmation:
                escalation_id = self._escalate(
                    run_id=run_id,
                    goal=goal,
                    current_step=f"step_{step_index}",
                    reason=InterventionReason.RISKY_ACTION_CONFIRMATION,
                    reason_detail=policy_decision.reason,
                    state=state,
                )
                history.append("[human confirmed a risky action before it was executed]")

            result = self._surface.act(action)
            self._logger.log(
                "action_executed",
                step=step_index,
                action=action.model_dump(mode="json"),
                result=result.model_dump(mode="json"),
            )
            traces.append(StepTrace(step_index=step_index, state_before=state, action=action, result=result))
            history.append(f"{action.type.value} -> {result.outcome.value}: {result.detail or ''}")

            if result.outcome == ActionOutcome.FAILED:
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    stop_reason = DiscoveryStopReason.DEAD_END
                    break
            else:
                consecutive_failures = 0
        else:
            stop_reason = DiscoveryStopReason.MAX_STEPS

        outputs: dict[str, str] = {}
        for trace in traces:
            if trace.result.extracted:
                outputs.update(trace.result.extracted)

        discovery_result = DiscoveryResult(
            run_id=run_id,
            stop_reason=stop_reason,
            goal=goal,
            steps_taken=len(traces),
            escalation_id=escalation_id,
            outputs=outputs,
            summary=summary,
            capability_slug=capability_slug,
            capability_name=capability_name,
            capability_description=capability_description,
        )
        self._logger.save_json("discovery_result.json", discovery_result.model_dump(mode="json"))
        return discovery_result, traces, final_state

    def _escalate(
        self,
        *,
        run_id: str,
        goal: str,
        current_step: str,
        reason: InterventionReason,
        reason_detail: str,
        state: SurfaceState,
    ) -> str:
        before_ref = None
        if hasattr(self._surface, "save_screenshot"):
            before_ref = self._surface.save_screenshot("escalation_before")  # type: ignore[attr-defined]

        req = self._interventions.create(
            run_id=run_id,
            run_kind="discovery",
            goal_or_capability=goal,
            current_step=current_step,
            reason=reason,
            reason_detail=reason_detail,
            state_url=state.url,
            before_screenshot_ref=before_ref,
        )
        self._logger.log(
            "escalation_raised",
            intervention_id=req.id,
            reason=reason.value,
            detail=reason_detail,
            operator_url=f"{self._operator_base_url}/interventions/{req.id}",
        )
        print(f"\n[ESCALATION] {reason.value}: {reason_detail}")
        print(f"[ESCALATION] The live browser window is waiting. Operate it directly, then resume here:")
        print(f"[ESCALATION]   {self._operator_base_url}/interventions/{req.id}\n")

        self._interventions.wait_for_resume(req.id)

        after_ref = None
        if hasattr(self._surface, "save_screenshot"):
            after_ref = self._surface.save_screenshot("escalation_after")  # type: ignore[attr-defined]
        self._interventions.set_after_screenshot(req.id, after_ref)
        self._logger.log("escalation_resumed", intervention_id=req.id)
        return req.id
