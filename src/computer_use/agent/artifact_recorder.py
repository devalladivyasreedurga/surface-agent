"""Turns a successful discovery run's step trace into a versioned CapabilityArtifact.

Two things this module deliberately does NOT do, both by design:

1. It does not persist anything to disk. Saving, deduplication, and
   versioning are CapabilityRegistry's job (see capability_registry.py) --
   this module only ever *builds* an artifact object from a trace. That
   split is what makes "run discovery twice, get one artifact" possible: the
   registry can compare two freshly-built candidates structurally before
   deciding whether either one needs to touch disk.
2. It does not carry discovery-run-specific data into the artifact: no raw
   LLM reasoning, no literal observed values, no goal text embedded in the
   description. The artifact is a reusable execution contract, not a
   transcript -- the transcript lives in the evidence log
   (evidence/discovery_<run_id>/log.jsonl), which already captures every
   action's original reasoning untouched.

Parameterization heuristic (documented trade-off, see REPORT.md): we look for
numeric tokens (3+ digits) in the goal text -- these are almost always the
identifiers a caller supplies (a member id, an account number). Wherever that
exact literal shows up again in a recorded action's value or URL, we replace
it with `{{input.<name>}}`, naming the parameter after the accessible
name/label of the field it was typed into (snake_cased), falling back to
`input_N`. This is simple and reliable for the common "one or two
caller-supplied identifiers" case this assignment's example goals all are;
it is not a general slot-filling NLP system.
"""
from __future__ import annotations

import re

from computer_use.agent.types import StepTrace
from computer_use.models.actions import ActionType, Locator
from computer_use.models.artifact import (
    ApplicationTarget,
    CapabilityArtifact,
    Checkpoint,
    InputParam,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
)
from computer_use.models.surface_state import SurfaceState
from computer_use.models.target_spec import TargetSpec
from computer_use.params import make_placeholder

_TOKEN_RE = re.compile(r"\d{3,}")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def sanitize_slug(text: str, fallback: str = "capability") -> str:
    """Machine-safe id from arbitrary (possibly model-generated) text.
    Used for capability_slug, which becomes part of a filename."""
    s = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    return s[:64] or fallback


def _snake_case(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()
    return s or "input"


def has_meaningful_replay_contract(artifact: CapabilityArtifact) -> bool:
    """Gate on whether an artifact is actually worth persisting as a reusable
    capability, not just whether discovery reported success.

    A discovery run can legitimately finish successfully without ever
    observing what a *found* result looks like -- e.g. the goal's identifier
    happens not to exist, and the agent correctly recognizes "not found" as
    the completed goal. Recording that run's 2-step trace (fill, click; no
    extraction) as a reusable capability would be actively harmful: its only
    checkpoint is "we ended up back on the search page", which is true
    whether the member was found, not found, or hit a validation error --
    replaying it can never come back anything but a false `success`, because
    it was never around to see the states it's supposed to be able to tell
    apart.

    A capability earns "reusable" by having at least one of:
      - a declared output backed by a real extraction step (the common
        case -- if it can read a value, checkpoint-vs-outcome ambiguity on
        the read step is instead caught by the FAILED extraction itself,
        which replay's business-outcome fallback already handles), or
      - a success checkpoint that asserts more than a bare URL match --
        any of `expect_element`, `expect_element_visible`,
        `expect_text_contains`, `expect_output_present`, or (recursively)
        an `any_of` alternative that itself qualifies. A route alone does
        not prove which outcome occurred on it; these are the condition
        types that actually distinguish states -- see models/artifact.py,
        Checkpoint.
    """
    has_extraction_output = any(step.extract_as for step in artifact.steps)
    return has_extraction_output or _has_strong_checkpoint(artifact.success_checkpoint)


def _has_strong_checkpoint(checkpoint: Checkpoint) -> bool:
    direct = bool(
        checkpoint.expect_element is not None
        or checkpoint.expect_element_visible is not None
        or checkpoint.expect_text_contains
        or checkpoint.expect_output_present
    )
    return direct or any(_has_strong_checkpoint(alt) for alt in checkpoint.any_of)


class ArtifactRecorder:
    def record(
        self,
        *,
        run_id: str,
        goal: str,
        target: TargetSpec,
        traces: list[StepTrace],
        final_state: SurfaceState | None,
        capability_id: str,
        capability_name: str,
        capability_description: str,
        app_key: str,
    ) -> CapabilityArtifact:
        tokens = _TOKEN_RE.findall(goal)
        token_to_param: dict[str, InputParam] = {}
        steps: list[Step] = []
        outputs: list[OutputField] = []

        def _param_for(token: str, label_hint: str) -> InputParam:
            if token in token_to_param:
                return token_to_param[token]
            name = _snake_case(label_hint) if label_hint else f"input_{len(token_to_param) + 1}"
            base_name, n = name, 2
            existing_names = {p.name for p in token_to_param.values()}
            while name in existing_names:
                name = f"{base_name}_{n}"
                n += 1
            param = InputParam(
                name=name,
                type=ParamType.STRING,
                description=f"Runtime value for '{label_hint}'" if label_hint else "Runtime input value",
                example=token,
            )
            token_to_param[token] = param
            return param

        def _parameterize(text: str | None, label_hint: str) -> str | None:
            if not text:
                return text
            out = text
            for token in tokens:
                if token in out:
                    param = _param_for(token, label_hint)
                    out = out.replace(token, make_placeholder(param.name))
            return out

        for i, trace in enumerate(traces):
            action = trace.action.model_copy(deep=True)
            step_id = f"step_{i + 1}"
            label_hint = ""
            if action.target is not None:
                label_hint = action.target.primary.value.split("|")[-1]

            if action.type in (ActionType.FILL, ActionType.NAVIGATE):
                action.value = _parameterize(action.value, label_hint)

            # The artifact is a reusable contract, not a transcript: the
            # model's per-action reasoning is discovery-run evidence, not
            # part of the recorded capability. It stays in log.jsonl.
            action.reasoning = None

            extract_as = None
            if action.type == ActionType.EXTRACT:
                extract_as = action.value
                outputs.append(
                    OutputField(
                        name=extract_as or f"output_{i + 1}",
                        type=ParamType.STRING,
                        source_step_id=step_id,
                        description=f"Extracted from '{label_hint}'" if label_hint else "Extracted value",
                    )
                )

            steps.append(
                Step(
                    id=step_id,
                    description=_describe(action, label_hint),
                    action=action,
                    risk=RiskLevel.SAFE,
                    checkpoint=None,
                    extract_as=extract_as,
                )
            )

        final_extract_locator = self._final_extract_locator(steps)
        self._attach_pre_extract_checkpoint(steps, final_extract_locator)
        success_checkpoint = self._infer_success_checkpoint(
            final_state, tokens, token_to_param, final_extract_locator
        )

        return CapabilityArtifact(
            capability_id=capability_id,
            name=capability_name,
            description=capability_description,
            target=ApplicationTarget(app_key=app_key, base_url=target.url, surface_type=target.type),
            inputs=list(token_to_param.values()),
            outputs=outputs,
            steps=steps,
            success_checkpoint=success_checkpoint,
            created_from_run_id=run_id,
        )

    def _final_extract_locator(self, steps: list[Step]) -> Locator | None:
        """The last extraction step's primary locator -- reused as the
        natural "did we actually reach a page where this value exists"
        checkpoint, both right before that step and as part of the overall
        success checkpoint. Real, observed-in-this-run structure, not an
        invented heuristic element."""
        for step in reversed(steps):
            if step.extract_as and step.action.target is not None:
                return step.action.target.primary
        return None

    def _attach_pre_extract_checkpoint(self, steps: list[Step], locator: Locator | None) -> None:
        if locator is None:
            return
        extract_index = next((i for i, s in enumerate(steps) if s.extract_as), None)
        if extract_index is None or extract_index == 0:
            return
        prior = steps[extract_index - 1]
        if prior.checkpoint is not None:
            return
        prior.checkpoint = Checkpoint(
            description=(
                "Reached a page where the value to extract is present "
                "(if not, replay will check for a known business outcome before failing)."
            ),
            expect_element=locator,
        )

    def _infer_success_checkpoint(
        self,
        final_state: SurfaceState | None,
        tokens: list[str],
        token_to_param: dict[str, InputParam],
        final_extract_locator: Locator | None,
    ) -> Checkpoint:
        """`expect_element` (when available) is the primary, discriminating
        signal -- it asserts a specific piece of state actually exists.
        `expect_url_contains` is included when the surface reported a
        meaningful (non-empty) URL, but only ever as a supplementary signal
        alongside it, never as the sole condition: has_meaningful_replay_
        contract() rejects any candidate whose only signal is a bare URL,
        because one route can represent many outcomes (see REPORT.md). A
        surface with no URL concept (SurfaceState.url == "") simply omits
        this field rather than recording a vacuous empty-string condition.
        """
        if final_state is None:
            return Checkpoint(description="Discovery run completed (no final state captured).")

        path_template: str | None = None
        if final_state.url:
            url_template = final_state.url
            for token, param in token_to_param.items():
                url_template = url_template.replace(token, make_placeholder(param.name))
            # Keep only the path portion for robustness against host/port differences.
            path_template = url_template.split("://", 1)[-1]
            path_template = "/" + path_template.split("/", 1)[1] if "/" in path_template else path_template

        description_parts = []
        if path_template:
            description_parts.append(f"reached the expected final page ({path_template})")
        if final_extract_locator is not None:
            description_parts.append("with the extracted value's element present")
        description = (" ".join(description_parts) or "discovery run completed").capitalize() + "."

        return Checkpoint(
            description=description,
            expect_url_contains=path_template,
            expect_element=final_extract_locator,
        )


def _describe(action, label_hint: str) -> str:
    if action.type == ActionType.NAVIGATE:
        return f"Navigate to {action.value}"
    if action.type == ActionType.FILL:
        return f"Fill '{label_hint}' with {action.value}"
    if action.type == ActionType.CLICK:
        return f"Click '{label_hint}'"
    if action.type == ActionType.EXTRACT:
        return f"Extract '{label_hint}' as output '{action.value}'"
    if action.type == ActionType.WAIT:
        return f"Wait {action.value}s"
    return f"{action.type.value} on '{label_hint}'"
