"""Deterministic replay: the production execution path.

Notably does NOT import anything from computer_use.llm. That is not a
convention, it is the whole guarantee -- there is no code path here that can
reach a model, so "replay makes zero LLM calls" holds by construction, not by
discipline.
"""
from __future__ import annotations

from computer_use.evidence.logger import RunLogger
from computer_use.escalation.intervention import InterventionManager, InterventionReason
from computer_use.models.actions import Action, ActionOutcome, ActionType, Locator, Target
from computer_use.models.artifact import CapabilityArtifact, Checkpoint
from computer_use.models.results import (
    BusinessOutcomeDetail,
    HardFailureDetail,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
)
from computer_use import params
from computer_use.replay.error_taxonomy import detect_business_outcome
from computer_use.safety.policy import PolicyEngine
from computer_use.surface.base import Surface


class ReplayEngine:
    def __init__(
        self,
        *,
        surface: Surface,
        policy: PolicyEngine,
        interventions: InterventionManager,
        logger: RunLogger,
        operator_base_url: str,
    ):
        self._surface = surface
        self._policy = policy
        self._interventions = interventions
        self._logger = logger
        self._operator_base_url = operator_base_url

    def run(
        self,
        *,
        artifact: CapabilityArtifact,
        inputs: dict[str, str],
        base_url_override: str | None = None,
    ) -> ReplayResult:
        run_id = self._logger.run_id
        self._logger.log("replay_started", run_id=run_id, capability_id=artifact.capability_id, inputs=inputs)

        missing = self._validate_inputs(artifact, inputs)
        if missing:
            return self._hard_failure(
                run_id, artifact, steps_completed=0,
                step_id="input_validation",
                expected=f"required inputs: {sorted(p.name for p in artifact.inputs if p.required)}",
                observed=f"provided inputs: {sorted(inputs.keys())}",
                error_code="missing_required_input",
                message=f"missing required input parameter(s): {sorted(missing)}",
            )

        base_url = base_url_override or artifact.target.base_url
        self._logger.log("initial_navigation", url=base_url)
        self._surface.act(Action(type=ActionType.NAVIGATE, value=base_url, reasoning="replay initial navigation"))

        recovery_events: list[RecoveryEvent] = []
        outputs: dict[str, str] = {}
        steps_completed = 0

        for step in artifact.steps:
            try:
                resolved_action = self._resolve_action(step.action, inputs)
            except KeyError as e:
                return self._hard_failure(
                    run_id, artifact, steps_completed,
                    step_id=step.id, expected="all input placeholders resolvable",
                    observed=str(e), error_code="param_resolution_error", message=str(e),
                )

            current_url = self._current_url()
            policy_decision = self._policy.evaluate(resolved_action, current_url=current_url, step_risk=step.risk)
            if not policy_decision.allowed:
                self._logger.log("policy_blocked", step=step.id, reason=policy_decision.reason)
                return self._hard_failure(
                    run_id, artifact, steps_completed,
                    step_id=step.id, expected="action permitted by policy",
                    observed=policy_decision.reason, error_code="policy_blocked", message=policy_decision.reason,
                )

            if policy_decision.requires_confirmation:
                self._escalate_for_confirmation(run_id, artifact, step.id, policy_decision.reason)

            result = self._surface.act(resolved_action)
            self._logger.log(
                "action_executed", step=step.id,
                action=resolved_action.model_dump(mode="json"), result=result.model_dump(mode="json"),
            )

            if result.outcome == ActionOutcome.RECOVERED:
                recovery_events.append(
                    RecoveryEvent(step_id=step.id, condition=result.recovery_note or "recovered",
                                  action_taken=result.recovery_note or "retried", attempt=1)
                )

            if result.outcome == ActionOutcome.FAILED:
                business = self._check_business_outcome()
                if business:
                    return self._business_outcome_result(run_id, artifact, steps_completed, step.id, business, recovery_events)
                return self._hard_failure(
                    run_id, artifact, steps_completed,
                    step_id=step.id, expected=step.description, observed=result.detail or "action failed",
                    error_code="action_failed", message=result.detail or "action failed",
                )

            if step.extract_as and result.extracted:
                outputs[step.extract_as] = result.extracted.get(step.extract_as, "")

            if step.checkpoint and not step.checkpoint.is_empty():
                ok, detail, recovered = self._check_checkpoint(step.checkpoint, inputs, outputs)
                if ok and recovered:
                    recovery_events.append(
                        RecoveryEvent(
                            step_id=step.id, condition="known_interstitial_dismissed",
                            action_taken="known_interstitial_dismissed", attempt=1,
                        )
                    )
                if not ok:
                    business = self._check_business_outcome()
                    if business:
                        return self._business_outcome_result(run_id, artifact, steps_completed, step.id, business, recovery_events)
                    return self._hard_failure(
                        run_id, artifact, steps_completed,
                        step_id=step.id, expected=step.checkpoint.description, observed=detail,
                        error_code="checkpoint_failed", message=detail,
                    )

            steps_completed += 1

        ok, detail, recovered = self._check_checkpoint(artifact.success_checkpoint, inputs, outputs)
        if ok and recovered:
            recovery_events.append(
                RecoveryEvent(
                    step_id="success_checkpoint", condition="known_interstitial_dismissed",
                    action_taken="known_interstitial_dismissed", attempt=1,
                )
            )
        if not ok:
            business = self._check_business_outcome()
            if business:
                return self._business_outcome_result(run_id, artifact, steps_completed, "success_checkpoint", business, recovery_events)
            return self._hard_failure(
                run_id, artifact, steps_completed,
                step_id="success_checkpoint", expected=artifact.success_checkpoint.description, observed=detail,
                error_code="success_checkpoint_failed", message=detail,
            )

        result = ReplayResult(
            status=ReplayStatus.SUCCESS,
            capability_id=artifact.capability_id,
            capability_version=artifact.capability_version,
            run_id=run_id,
            outputs=outputs,
            recovery_events=recovery_events,
            steps_completed=steps_completed,
            steps_total=len(artifact.steps),
        )
        self._logger.save_json("replay_result.json", result.model_dump(mode="json"))
        return result

    # ---- helpers -----------------------------------------------------

    def _validate_inputs(self, artifact: CapabilityArtifact, inputs: dict[str, str]) -> set[str]:
        required = {p.name for p in artifact.inputs if p.required}
        return required - inputs.keys()

    def _resolve_action(self, action: Action, inputs: dict[str, str]) -> Action:
        resolved = action.model_copy(deep=True)
        resolved.value = params.resolve(resolved.value, inputs)
        if resolved.target is not None:
            resolved.target = Target(
                primary=self._resolve_locator(resolved.target.primary, inputs),
                fallbacks=[self._resolve_locator(l, inputs) for l in resolved.target.fallbacks],
            )
        return resolved

    def _resolve_locator(self, locator: Locator, inputs: dict[str, str]) -> Locator:
        new = locator.model_copy(deep=True)
        new.value = params.resolve(new.value, inputs) or new.value
        return new

    def _current_url(self) -> str:
        try:
            return self._surface.observe().url
        except Exception:
            return ""

    def _check_checkpoint(
        self, checkpoint: Checkpoint, inputs: dict[str, str], outputs: dict[str, str]
    ) -> tuple[bool, str, bool]:
        """A checkpoint passes if its own directly-set conditions all pass
        (the implicit AND), OR if any one of its `any_of` alternatives would
        pass on its own (recursive -- an alternative may have its own
        any_of). This is the whole combinator model: no separate `all_of` is
        needed because "multiple fields set on one Checkpoint" already means
        that."""
        ok, detail, recovered = self._check_checkpoint_direct(checkpoint, inputs, outputs)
        if ok:
            return ok, detail, recovered
        for alternative in checkpoint.any_of:
            alt_ok, alt_detail, alt_recovered = self._check_checkpoint(alternative, inputs, outputs)
            if alt_ok:
                return True, alt_detail, alt_recovered
        return ok, detail, recovered

    def _check_checkpoint_direct(
        self, checkpoint: Checkpoint, inputs: dict[str, str], outputs: dict[str, str]
    ) -> tuple[bool, str, bool]:
        if checkpoint.expect_output_present:
            # Engine-level condition -- no Surface call. Asserts replay's own
            # bookkeeping already has this value, independent of whatever the
            # current page (if there even is one) looks like.
            name = checkpoint.expect_output_present
            if not outputs.get(name):
                return False, f"expected output '{name}' to already be present", False

        url_contains = params.resolve(checkpoint.expect_url_contains, inputs)
        text_contains = params.resolve(checkpoint.expect_text_contains, inputs)
        element = self._resolve_locator(checkpoint.expect_element, inputs) if checkpoint.expect_element is not None else None
        element_visible = (
            self._resolve_locator(checkpoint.expect_element_visible, inputs)
            if checkpoint.expect_element_visible is not None else None
        )

        if url_contains or text_contains or element is not None or element_visible is not None:
            if hasattr(self._surface, "check_condition"):
                return self._surface.check_condition(  # type: ignore[attr-defined]
                    url_contains=url_contains, element=element, element_visible=element_visible, text_contains=text_contains,
                )
            return True, "ok (surface does not support checkpoint verification)", False

        return True, "ok", False

    def _check_business_outcome(self) -> tuple[str, str] | None:
        state = self._surface.observe()
        return detect_business_outcome(state.visible_text_excerpt or "")

    def _business_outcome_result(self, run_id, artifact, steps_completed, step_id, business, recovery_events) -> ReplayResult:
        code, message = business
        result = ReplayResult(
            status=ReplayStatus.BUSINESS_OUTCOME,
            capability_id=artifact.capability_id,
            capability_version=artifact.capability_version,
            run_id=run_id,
            business_outcome=BusinessOutcomeDetail(code=code, message=message, step_id=step_id),
            recovery_events=recovery_events,
            steps_completed=steps_completed,
            steps_total=len(artifact.steps),
        )
        self._logger.log("replay_business_outcome", step=step_id, code=code, message=message)
        self._logger.save_json("replay_result.json", result.model_dump(mode="json"))
        return result

    def _hard_failure(self, run_id, artifact, steps_completed, *, step_id, expected, observed, error_code, message) -> ReplayResult:
        evidence_ref = None
        if hasattr(self._surface, "save_screenshot"):
            evidence_ref = self._surface.save_screenshot(f"hard_failure_{step_id}")  # type: ignore[attr-defined]
        result = ReplayResult(
            status=ReplayStatus.HARD_FAILURE,
            capability_id=artifact.capability_id,
            capability_version=artifact.capability_version,
            run_id=run_id,
            hard_failure=HardFailureDetail(
                step_id=step_id, expected=expected, observed=observed,
                error_code=error_code, message=message, evidence_ref=evidence_ref,
            ),
            steps_completed=steps_completed,
            steps_total=len(artifact.steps),
        )
        self._logger.log("replay_hard_failure", step=step_id, error_code=error_code, message=message, evidence_ref=evidence_ref)
        self._logger.save_json("replay_result.json", result.model_dump(mode="json"))
        return result

    def _escalate_for_confirmation(self, run_id: str, artifact: CapabilityArtifact, step_id: str, reason: str) -> None:
        before_ref = None
        if hasattr(self._surface, "save_screenshot"):
            before_ref = self._surface.save_screenshot("confirmation_before")  # type: ignore[attr-defined]
        req = self._interventions.create(
            run_id=run_id, run_kind="replay", goal_or_capability=artifact.capability_id,
            current_step=step_id, reason=InterventionReason.RISKY_ACTION_CONFIRMATION,
            reason_detail=reason, state_url=self._current_url(), before_screenshot_ref=before_ref,
        )
        self._logger.log(
            "escalation_raised", intervention_id=req.id, reason=req.reason.value, detail=reason,
            operator_url=f"{self._operator_base_url}/interventions/{req.id}",
        )
        print(f"\n[ESCALATION] risky action requires confirmation: {reason}")
        print(f"[ESCALATION]   {self._operator_base_url}/interventions/{req.id}\n")

        self._interventions.wait_for_resume(req.id)

        after_ref = None
        if hasattr(self._surface, "save_screenshot"):
            after_ref = self._surface.save_screenshot("confirmation_after")  # type: ignore[attr-defined]
        self._interventions.set_after_screenshot(req.id, after_ref)
        self._logger.log("escalation_resumed", intervention_id=req.id)
