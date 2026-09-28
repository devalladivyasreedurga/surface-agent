from computer_use.agent.artifact_recorder import ArtifactRecorder, sanitize_slug
from computer_use.agent.types import StepTrace
from computer_use.models.actions import (
    Action, ActionOutcome, ActionResult, ActionType, Locator, LocatorStrategy, Target,
)
from computer_use.models.surface_state import SurfaceState
from computer_use.models.target_spec import TargetSpec


def _trace(i, action, result) -> StepTrace:
    return StepTrace(
        step_index=i,
        state_before=SurfaceState(url="http://127.0.0.1:8000/", title="Member Search"),
        action=action,
        result=result,
    )


def _traces():
    fill = Action(
        type=ActionType.FILL,
        target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="textbox|Member ID")),
        value="10001",
        reasoning="I need to enter member ID 10001 to search for this member.",
    )
    click = Action(
        type=ActionType.CLICK,
        target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Search")),
        reasoning="Now I'll click Search to look up member 10001.",
    )
    extract = Action(
        type=ActionType.EXTRACT,
        target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value")),
        value="savings_balance",
        reasoning="The savings balance for member 10001 is $4230.55, extracting it now.",
    )
    return [
        _trace(0, fill, ActionResult(outcome=ActionOutcome.OK, detail="filled")),
        _trace(1, click, ActionResult(outcome=ActionOutcome.OK, detail="clicked")),
        _trace(2, extract, ActionResult(outcome=ActionOutcome.OK, extracted={"savings_balance": "$4230.55"})),
    ]


def _record(**overrides):
    recorder = ArtifactRecorder()
    defaults = dict(
        run_id="run1",
        goal="Look up member 10001 and read the savings balance",
        target=TargetSpec(type="web", url="http://127.0.0.1:8000"),
        traces=_traces(),
        final_state=SurfaceState(url="http://127.0.0.1:8000/members/10001", title="Jordan Rivera"),
        capability_id="lookup_member_savings_balance",
        capability_name="Look up member savings balance",
        capability_description="Looks up a member by ID and returns their savings balance.",
        app_key="mockbank",
    )
    defaults.update(overrides)
    return recorder.record(**defaults)


def test_recorder_parameterizes_the_goal_supplied_identifier():
    artifact = _record()

    assert len(artifact.inputs) == 1
    param = artifact.inputs[0]
    assert param.name == "member_id"
    assert param.example == "10001"

    fill_step = artifact.steps[0]
    assert fill_step.action.value == "{{input.member_id}}"

    for step in artifact.steps:
        if step.action.value:
            assert "10001" not in step.action.value

    assert artifact.success_checkpoint.expect_url_contains == "/members/{{input.member_id}}"


def test_recorder_captures_extraction_as_output():
    artifact = _record()
    assert len(artifact.outputs) == 1
    assert artifact.outputs[0].name == "savings_balance"
    assert artifact.outputs[0].source_step_id == "step_3"


def test_recorder_uses_generic_name_and_description_not_raw_goal():
    artifact = _record()
    assert artifact.name == "Look up member savings balance"
    assert artifact.description == "Looks up a member by ID and returns their savings balance."
    assert "10001" not in artifact.name
    assert "10001" not in artifact.description


def test_recorder_strips_llm_reasoning_from_every_step():
    artifact = _record()
    for step in artifact.steps:
        assert step.action.reasoning is None
    # and none of the discovery-run reasoning text leaked in anywhere else
    dumped = artifact.model_dump_json()
    assert "extracting it now" not in dumped
    assert "$4230.55" not in dumped


def test_recorder_attaches_checkpoint_before_final_extract_step():
    artifact = _record()
    click_step = artifact.steps[1]  # click 'Search', immediately before the extract step
    assert click_step.checkpoint is not None
    assert click_step.checkpoint.expect_element is not None
    assert click_step.checkpoint.expect_element.value == "cell|Savings Balance Value"


def test_recorder_attaches_element_checkpoint_to_success_checkpoint_not_just_url():
    artifact = _record()
    assert artifact.success_checkpoint.expect_url_contains is not None
    assert artifact.success_checkpoint.expect_element is not None
    assert artifact.success_checkpoint.expect_element.value == "cell|Savings Balance Value"


def test_sanitize_slug_produces_safe_identifiers():
    assert sanitize_slug("Lookup Member Savings Balance") == "lookup_member_savings_balance"
    assert sanitize_slug("  weird!! Chars--here  ") == "weird_chars_here"
    assert sanitize_slug("") == "capability"
