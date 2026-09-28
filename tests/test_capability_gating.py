"""Regression coverage for the capability-gating fix: a discovery run that
completes successfully but never observes a "found" state (e.g. the goal's
identifier doesn't exist) must not be persisted as a reusable capability.

Runs a real mock_bank instance and a real Playwright browser -- the LLM is
the only thing faked, via a small scripted LLMClient, so the rest of the
system (Surface, PolicyEngine, ArtifactRecorder, CapabilityRegistry,
ReplayEngine) is exercised for real, same as any other run.
"""
from __future__ import annotations

import threading
import time

import pytest
import uvicorn

from computer_use import services
from computer_use.agent.capability_registry import CapabilityRegistry
from computer_use.agent.types import AgentDecision, DecisionKind
from computer_use.escalation.intervention import InterventionManager
from computer_use.llm.base import LLMClient
from computer_use.models.actions import Action, ActionType, Locator, LocatorStrategy, Target
from computer_use.models.artifact import (
    ApplicationTarget, CapabilityArtifact, Checkpoint, InputParam, OutputField, ParamType, RiskLevel, Step,
)
from computer_use.models.results import ReplayStatus
from mock_bank.app import app as mock_bank_app

_PORT = 8091
_BASE_URL = f"http://127.0.0.1:{_PORT}"
_OPERATOR_BASE_URL = "http://127.0.0.1:0"  # unused unless an escalation fires, which these scripts never trigger
_CAPABILITY_ID = "lookup_member_savings_balance"


@pytest.fixture(scope="module")
def mock_bank_server():
    config = uvicorn.Config(mock_bank_app, host="127.0.0.1", port=_PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield _BASE_URL
    server.should_exit = True
    thread.join(timeout=5)


class ScriptedLLMClient(LLMClient):
    """Returns a pre-scripted sequence of decisions instead of calling an
    API -- deterministic and free, and exercises the exact same DiscoveryAgent
    code path a real AnthropicLLMClient would."""

    def __init__(self, decisions: list[AgentDecision]):
        self._decisions = list(decisions)

    def decide(self, *, goal, state, history):
        if not self._decisions:
            return AgentDecision(kind=DecisionKind.FINISH_STUCK, reasoning="script exhausted")
        return self._decisions.pop(0)


def _fill_member_id(member_id: str) -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.ACTION,
        reasoning="fill member id",
        action=Action(
            type=ActionType.FILL,
            target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="textbox|Member ID")),
            value=member_id,
        ),
    )


def _click_search() -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.ACTION,
        reasoning="click search",
        action=Action(
            type=ActionType.CLICK,
            target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Search")),
        ),
    )


def _extract_balance() -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.ACTION,
        reasoning="extract balance",
        action=Action(
            type=ActionType.EXTRACT,
            target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value")),
            value="savings_balance",
        ),
    )


def _finish_success(summary: str) -> AgentDecision:
    return AgentDecision(
        kind=DecisionKind.FINISH_SUCCESS,
        reasoning="goal achieved",
        summary=summary,
        capability_slug=_CAPABILITY_ID,
        capability_name="Look up member savings balance",
        capability_description="Looks up a member by ID and returns their savings balance.",
    )


def _not_found_script() -> ScriptedLLMClient:
    return ScriptedLLMClient([
        _fill_member_id("99999"),
        _click_search(),
        _finish_success("Member 99999 was not found."),
    ])


def _found_script() -> ScriptedLLMClient:
    return ScriptedLLMClient([
        _fill_member_id("10001"),
        _click_search(),
        _extract_balance(),
        _finish_success("Member 10001's savings balance is $4230.55."),
    ])


def _well_formed_artifact(version: int = 1) -> CapabilityArtifact:
    """Mirrors the real, LLM-recorded v1 artifact's structure: an extraction
    step and a checkpoint that actually asserts an element, not just a URL."""
    balance_locator = Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Savings Balance Value")
    return CapabilityArtifact(
        capability_id=_CAPABILITY_ID,
        capability_version=version,
        name="Look up member savings balance",
        description="Looks up a member by ID and returns their savings balance.",
        target=ApplicationTarget(app_key="mockbank", base_url=_BASE_URL, surface_type="web"),
        inputs=[InputParam(name="member_id", type=ParamType.STRING, example="10001")],
        outputs=[OutputField(name="savings_balance", type=ParamType.STRING, source_step_id="step_3")],
        steps=[
            Step(
                id="step_1", description="Fill Member ID",
                action=Action(type=ActionType.FILL, target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="textbox|Member ID")), value="{{input.member_id}}"),
                risk=RiskLevel.SAFE,
            ),
            Step(
                id="step_2", description="Click Search",
                action=Action(type=ActionType.CLICK, target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Search"))),
                risk=RiskLevel.SAFE,
                checkpoint=Checkpoint(description="reached details", expect_element=balance_locator),
            ),
            Step(
                id="step_3", description="Extract balance",
                action=Action(type=ActionType.EXTRACT, target=Target(primary=balance_locator), value="savings_balance"),
                risk=RiskLevel.SAFE, extract_as="savings_balance",
            ),
        ],
        success_checkpoint=Checkpoint(
            description="reached member page with balance visible",
            expect_url_contains="/members/{{input.member_id}}",
            expect_element=balance_locator,
        ),
        created_from_run_id="seed",
    )


def test_discovery_against_nonexistent_member_produces_no_artifact(tmp_path, mock_bank_server):
    artifacts_dir = tmp_path / "artifacts"
    evidence_dir = tmp_path / "evidence"

    outcome = services.discover_and_record(
        target_url=mock_bank_server,
        goal="Look up member 99999 and read the savings balance",
        interventions=InterventionManager(),
        operator_base_url=_OPERATOR_BASE_URL,
        artifacts_dir=artifacts_dir,
        evidence_dir=evidence_dir,
        headless=True,
        llm=_not_found_script(),
    )

    assert outcome.artifact is None
    assert outcome.artifact_created is False
    assert "MEMBER_NOT_FOUND" in outcome.artifact_reason
    assert "no reusable capability was recorded" in outcome.artifact_reason

    # no artifact file was written anywhere
    assert list(artifacts_dir.glob("*.json")) == []

    # but evidence for the run itself is still there
    assert outcome.evidence_dir.exists()
    log_path = outcome.evidence_dir / "log.jsonl"
    assert log_path.exists()
    assert "no_capability_recorded" in log_path.read_text()


def test_existing_v1_remains_unchanged_after_a_gated_run(tmp_path, mock_bank_server):
    artifacts_dir = tmp_path / "artifacts"
    registry = CapabilityRegistry(artifacts_dir)
    registry.save(_well_formed_artifact(version=1))
    v1_path = registry.path_for(_CAPABILITY_ID, 1)
    original_content = v1_path.read_text()

    outcome = services.discover_and_record(
        target_url=mock_bank_server,
        goal="Look up member 99999 and read the savings balance",
        interventions=InterventionManager(),
        operator_base_url=_OPERATOR_BASE_URL,
        artifacts_dir=artifacts_dir,
        evidence_dir=tmp_path / "evidence",
        headless=True,
        llm=_not_found_script(),
    )

    assert outcome.artifact is None
    assert v1_path.read_text() == original_content  # byte-for-byte unchanged
    assert list(artifacts_dir.glob(f"{_CAPABILITY_ID}.v*.json")) == [v1_path]  # no v2 created


def test_replay_of_v1_against_nonexistent_member_still_returns_business_outcome(tmp_path, mock_bank_server):
    artifact = _well_formed_artifact(version=1)

    outcome = services.replay_capability(
        artifact=artifact,
        inputs={"member_id": "99999"},
        interventions=InterventionManager(),
        operator_base_url=_OPERATOR_BASE_URL,
        target_url_override=mock_bank_server,
        evidence_dir=tmp_path / "evidence",
        headless=True,
    )

    assert outcome.result.status == ReplayStatus.BUSINESS_OUTCOME
    assert outcome.result.business_outcome.code == "MEMBER_NOT_FOUND"


def test_discovery_against_a_real_member_is_still_recorded_as_a_capability(tmp_path, mock_bank_server):
    """The gate must admit well-formed captures, not just reject bad ones."""
    artifacts_dir = tmp_path / "artifacts"

    outcome = services.discover_and_record(
        target_url=mock_bank_server,
        goal="Look up member 10001 and read the savings balance",
        interventions=InterventionManager(),
        operator_base_url=_OPERATOR_BASE_URL,
        artifacts_dir=artifacts_dir,
        evidence_dir=tmp_path / "evidence",
        headless=True,
        llm=_found_script(),
    )

    assert outcome.artifact is not None
    assert outcome.artifact_created is True
    assert outcome.artifact.capability_version == 1
    assert list(artifacts_dir.glob("*.json")) != []
