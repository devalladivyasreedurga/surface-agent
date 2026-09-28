"""The capability artifact: a typed, versioned, agent-invocable contract.

This is the reusable output of a discovery run and the sole input to replay.
It is deliberately NOT a Playwright script or a raw action transcript --
storing executable code would couple replay to one surface implementation and
would make review/diffing by a human much harder. A step here says *what* to
do and *how to find the control*, in surface-agnostic terms; the concrete
execution is left to whatever `Surface` implementation replay is given.

Design choices worth flagging (see REPORT.md for the full rationale):
  - Parameterization: recorded runtime values (e.g. "10001") are rewritten to
    `{{input.member_id}}` placeholders at record time, never stored literally.
    This keeps concrete PII out of the reusable artifact -- but it is not the
    whole PII story, see safety/redaction.py for the log/evidence side.
  - Risk classification lives on the step, not just the artifact, because a
    single capability can contain both safe reads and one irreversible write
    (e.g. "open sub-account and reach confirmation" stops just short of
    submit). The policy engine keys off the step's risk, not the capability's.
  - Every step that matters gets its own checkpoint, in addition to one
    top-level success checkpoint, so replay can localize *where* a flow
    diverged from the recorded happy path instead of only knowing that it did.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from computer_use.models.actions import Action, ActionType, Locator


class RiskLevel(str, Enum):
    SAFE = "safe"                  # read-only / trivially reversible (search, navigate, read)
    REVERSIBLE = "reversible"      # writes a draft/staged state that can be abandoned (fill a form)
    IRREVERSIBLE = "irreversible"  # money movement, account closure, final submit -- see PolicyEngine


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    description: str = ""
    required: bool = True
    example: str | None = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str = ""
    source_step_id: str = Field(..., description="Which step's extraction populates this output.")


class Checkpoint(BaseModel):
    """An assertable condition confirming the flow actually reached the
    expected state, rather than assuming the previous action worked."""

    description: str

    # Surface-observable conditions. All directly-set fields on one
    # Checkpoint are ANDed together -- there is no separate `all_of`, since
    # that is exactly what having multiple fields set already means. None of
    # these is privileged as "the" success signal; a checkpoint with only
    # `expect_url_contains` set is deliberately treated as weak (see
    # artifact_recorder.has_meaningful_replay_contract) because one route can
    # represent many outcomes -- URL is one optional condition among several,
    # not a default.
    expect_url_contains: str | None = None
    expect_element: Locator | None = Field(
        default=None,
        description="Element resolves in the accessibility tree / DOM. Presence, not visibility -- a display:none element still passes this one.",
    )
    expect_element_visible: Locator | None = Field(
        default=None,
        description="Element resolves AND is actually visible on screen (not display:none/hidden). Stricter than expect_element.",
    )
    expect_text_contains: str | None = Field(
        default=None,
        description="Substring of the page's rendered (visible) text -- Playwright's inner_text() already excludes hidden text, so this is effectively a 'text visible' check, not just 'text present in markup'.",
    )

    # Engine-level condition, not a Surface call: asserts that a named
    # output has already been captured by an earlier step in this replay run.
    # Lets a checkpoint assert "we have the value we came for" independently
    # of any page state at all -- the one condition type that works
    # identically whether or not the surface has any notion of a URL.
    expect_output_present: str | None = Field(
        default=None, description="Name of a declared output that must already have a non-empty value."
    )

    # Alternatives: if the directly-set fields above don't all pass, try each
    # of these in turn: the checkpoint as a whole passes if any one of them
    # would pass on its own (recursively -- an alternative may itself have
    # its own any_of). Lets a capability express "either this element or
    # that different element" without inventing a separate boolean-expression
    # schema.
    any_of: list["Checkpoint"] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.expect_url_contains,
            self.expect_element,
            self.expect_element_visible,
            self.expect_text_contains,
            self.expect_output_present,
            self.any_of,
        ])


class Step(BaseModel):
    id: str
    description: str
    action: Action
    risk: RiskLevel = RiskLevel.SAFE
    checkpoint: Checkpoint | None = None
    extract_as: str | None = Field(
        default=None, description="If this step extracts data, the output field name it feeds."
    )


class ApplicationTarget(BaseModel):
    """Metadata about the app this capability was recorded against.

    `app_key` is the vendor/product identity (e.g. "mockbank-teller-console")
    separate from `base_url`, which is tenant-specific. Replay against a
    different tenant of the *same* app_key is the intended reuse path (see
    REPORT.md, Heterogeneity & multi-tenant); replay refuses silently reusing
    an artifact whose app_key doesn't match the target unless the caller
    explicitly forces it.
    """

    app_key: str
    base_url: str
    surface_type: str = "web"


class CapabilityArtifact(BaseModel):
    schema_version: str = "1.0"
    capability_id: str
    capability_version: int = 1
    name: str
    description: str = ""

    target: ApplicationTarget

    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)

    steps: list[Step]
    success_checkpoint: Checkpoint

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_from_run_id: str = Field(..., description="Discovery run id that first created this version.")
    additional_run_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Discovery run ids that independently re-recorded a structurally "
            "identical flow and were deduplicated into this version instead of "
            "producing a new artifact file. Provenance only -- see CapabilityRegistry."
        ),
    )
    review_notes: str | None = None

    def max_risk(self) -> RiskLevel:
        order = [RiskLevel.SAFE, RiskLevel.REVERSIBLE, RiskLevel.IRREVERSIBLE]
        return max((s.risk for s in self.steps), key=order.index, default=RiskLevel.SAFE)

    def input_names(self) -> set[str]:
        return {p.name for p in self.inputs}
