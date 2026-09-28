from pathlib import Path

from computer_use.agent.capability_registry import CapabilityRegistry
from computer_use.models.actions import Action, ActionType, Locator, LocatorStrategy, Target
from computer_use.models.artifact import (
    ApplicationTarget, CapabilityArtifact, Checkpoint, InputParam, OutputField, ParamType, RiskLevel, Step,
)


def _artifact(*, example_value="10001", extra_step=False, description="v1 description") -> CapabilityArtifact:
    steps = [
        Step(
            id="step_1",
            description="Fill Member ID",
            action=Action(
                type=ActionType.FILL,
                target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="textbox|Member ID")),
                value="{{input.member_id}}",
            ),
            risk=RiskLevel.SAFE,
        ),
        Step(
            id="step_2",
            description="Click Search",
            action=Action(
                type=ActionType.CLICK,
                target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Search")),
            ),
            risk=RiskLevel.SAFE,
            checkpoint=Checkpoint(
                description="reached details",
                expect_element=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value"),
            ),
        ),
        Step(
            id="step_3",
            description="Extract balance",
            action=Action(
                type=ActionType.EXTRACT,
                target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value")),
                value="savings_balance",
            ),
            risk=RiskLevel.SAFE,
            extract_as="savings_balance",
        ),
    ]
    if extra_step:
        steps.append(
            Step(
                id="step_4",
                description="Click something new",
                action=Action(
                    type=ActionType.CLICK,
                    target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Extra")),
                ),
                risk=RiskLevel.SAFE,
            )
        )
    return CapabilityArtifact(
        capability_id="lookup_member_savings_balance",
        capability_version=1,
        name="Look up member savings balance",
        description=description,
        target=ApplicationTarget(app_key="mockbank", base_url="http://127.0.0.1:8000", surface_type="web"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, example=example_value)],
        outputs=[OutputField(name="savings_balance", type=ParamType.STRING, source_step_id="step_3")],
        steps=steps,
        success_checkpoint=Checkpoint(
            description="done",
            expect_url_contains="/members/{{input.member_id}}",
            expect_element=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value"),
        ),
        created_from_run_id="run1",
    )


def test_first_reconcile_creates_v1(tmp_path: Path):
    registry = CapabilityRegistry(tmp_path)
    artifact, created, reason = registry.reconcile(_artifact(), run_id="run1")
    assert created
    assert artifact.capability_version == 1
    assert (tmp_path / "lookup_member_savings_balance.v1.json").exists()


def test_structurally_identical_run_is_deduplicated_not_versioned(tmp_path: Path):
    registry = CapabilityRegistry(tmp_path)
    registry.reconcile(_artifact(example_value="10001"), run_id="run1")

    # Same structure, different example value (a different member id was
    # used this time) and a different LLM-phrased description -- must still
    # be treated as the same capability.
    candidate = _artifact(example_value="20002", description="a differently worded description")
    artifact, created, reason = registry.reconcile(candidate, run_id="run2")

    assert not created
    assert artifact.capability_version == 1
    assert "run2" in artifact.additional_run_ids
    # only one file on disk -- no v2 was created
    assert list(tmp_path.glob("lookup_member_savings_balance.v*.json")) == [
        tmp_path / "lookup_member_savings_balance.v1.json"
    ]


def test_reconciling_the_same_run_id_twice_does_not_duplicate_provenance(tmp_path: Path):
    registry = CapabilityRegistry(tmp_path)
    registry.reconcile(_artifact(), run_id="run1")
    artifact, created, _ = registry.reconcile(_artifact(example_value="99999"), run_id="run1")
    assert not created
    assert artifact.additional_run_ids.count("run1") == 0  # run1 is created_from_run_id, not re-added


def test_structurally_different_flow_creates_new_version(tmp_path: Path):
    registry = CapabilityRegistry(tmp_path)
    registry.reconcile(_artifact(), run_id="run1")

    candidate = _artifact(extra_step=True)
    artifact, created, reason = registry.reconcile(candidate, run_id="run2")

    assert created
    assert artifact.capability_version == 2
    assert (tmp_path / "lookup_member_savings_balance.v2.json").exists()
    assert (tmp_path / "lookup_member_savings_balance.v1.json").exists()  # v1 untouched


def test_list_capabilities_returns_latest_version_only(tmp_path: Path):
    registry = CapabilityRegistry(tmp_path)
    registry.reconcile(_artifact(), run_id="run1")
    registry.reconcile(_artifact(extra_step=True), run_id="run2")

    caps = registry.list_capabilities()
    assert len(caps) == 1
    assert caps[0].capability_version == 2


def test_get_specific_version(tmp_path: Path):
    registry = CapabilityRegistry(tmp_path)
    registry.reconcile(_artifact(), run_id="run1")
    registry.reconcile(_artifact(extra_step=True), run_id="run2")

    v1 = registry.get("lookup_member_savings_balance", version=1)
    v2 = registry.get("lookup_member_savings_balance", version=2)
    latest = registry.get("lookup_member_savings_balance")

    assert v1.capability_version == 1
    assert v2.capability_version == 2
    assert latest.capability_version == 2
