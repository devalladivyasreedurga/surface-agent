"""Checkpoint condition coverage that doesn't need a real browser: the
engine-level `expect_output_present` condition, `any_of` alternatives, the
meaningful-replay-contract gate recognizing the new condition types, and
structural fingerprinting differentiating artifacts that only differ in
them. Surface-observable conditions (url/element/element_visible/text) are
delegated to a stub Surface here -- their real Playwright behavior is
covered in tests/test_playwright_surface_recovery.py and
tests/test_playwright_surface_visibility.py.
"""
from pathlib import Path

from computer_use.agent.artifact_recorder import has_meaningful_replay_contract
from computer_use.agent.capability_registry import CapabilityRegistry, _fingerprint
from computer_use.escalation.intervention import InterventionManager
from computer_use.evidence.logger import RunLogger
from computer_use.models.actions import Action, ActionType, Locator, LocatorStrategy, Target
from computer_use.models.artifact import ApplicationTarget, CapabilityArtifact, Checkpoint, Step
from computer_use.models.surface_state import SurfaceState
from computer_use.replay.replay_engine import ReplayEngine
from computer_use.safety.policy import PolicyConfig, PolicyEngine
from computer_use.surface.base import Surface


class StubSurface(Surface):
    """A Surface whose check_condition is fully scripted, so tests can
    exercise ReplayEngine's checkpoint-combinator logic without a browser."""

    def __init__(self, check_condition_fn):
        self._check_condition_fn = check_condition_fn

    def observe(self) -> SurfaceState:
        return SurfaceState(url="http://example.test/", title="stub")

    def act(self, action):
        raise NotImplementedError("not exercised by these tests")

    def check_condition(self, *, url_contains, element, element_visible, text_contains):
        return self._check_condition_fn(url_contains, element, element_visible, text_contains)


def _engine(tmp_path: Path, check_condition_fn=lambda *a: (True, "ok", False)) -> ReplayEngine:
    logger = RunLogger(run_id="test", kind="replay", evidence_root=tmp_path)
    return ReplayEngine(
        surface=StubSurface(check_condition_fn),
        policy=PolicyEngine(PolicyConfig()),
        interventions=InterventionManager(),
        logger=logger,
        operator_base_url="http://127.0.0.1:0",
    )


# ---- Checkpoint.is_empty() ------------------------------------------------

def test_checkpoint_is_empty_true_with_nothing_set():
    assert Checkpoint(description="x").is_empty()


def test_checkpoint_is_not_empty_with_output_present():
    assert not Checkpoint(description="x", expect_output_present="balance").is_empty()


def test_checkpoint_is_not_empty_with_only_any_of():
    assert not Checkpoint(description="x", any_of=[Checkpoint(description="alt", expect_text_contains="y")]).is_empty()


# ---- expect_output_present (engine-level, no Surface call) ---------------

def test_output_present_passes_when_output_already_captured(tmp_path):
    engine = _engine(tmp_path)
    checkpoint = Checkpoint(description="x", expect_output_present="savings_balance")
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {"savings_balance": "$4230.55"})
    assert ok is True


def test_output_present_fails_when_output_missing(tmp_path):
    engine = _engine(tmp_path)
    checkpoint = Checkpoint(description="x", expect_output_present="savings_balance")
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {})
    assert ok is False
    assert "savings_balance" in detail


def test_output_present_fails_when_output_is_empty_string(tmp_path):
    engine = _engine(tmp_path)
    checkpoint = Checkpoint(description="x", expect_output_present="savings_balance")
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {"savings_balance": ""})
    assert ok is False


# ---- any_of combinator -----------------------------------------------------

def test_any_of_passes_when_an_alternative_passes(tmp_path):
    def fake_check(url_contains, element, element_visible, text_contains):
        return (text_contains == "ALT", f"checked {text_contains!r}", False)

    engine = _engine(tmp_path, fake_check)
    checkpoint = Checkpoint(
        description="primary",
        expect_text_contains="PRIMARY",
        any_of=[Checkpoint(description="alternative", expect_text_contains="ALT")],
    )
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {})
    assert ok is True


def test_any_of_fails_when_no_alternative_passes(tmp_path):
    engine = _engine(tmp_path, lambda *a: (False, "nope", False))
    checkpoint = Checkpoint(
        description="primary",
        expect_text_contains="PRIMARY",
        any_of=[Checkpoint(description="alternative", expect_text_contains="ALT")],
    )
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {})
    assert ok is False


def test_any_of_short_circuits_when_primary_already_passes(tmp_path):
    calls = []

    def fake_check(url_contains, element, element_visible, text_contains):
        calls.append(text_contains)
        return (text_contains == "PRIMARY", "ok", False)

    engine = _engine(tmp_path, fake_check)
    checkpoint = Checkpoint(
        description="primary",
        expect_text_contains="PRIMARY",
        any_of=[Checkpoint(description="alternative", expect_text_contains="ALT")],
    )
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {})
    assert ok is True
    assert calls == ["PRIMARY"]  # the alternative was never even evaluated


def test_any_of_nested_recursively(tmp_path):
    def fake_check(url_contains, element, element_visible, text_contains):
        return (text_contains == "DEEP", "ok", False)

    engine = _engine(tmp_path, fake_check)
    checkpoint = Checkpoint(
        description="primary", expect_text_contains="PRIMARY",
        any_of=[
            Checkpoint(
                description="mid", expect_text_contains="MID",
                any_of=[Checkpoint(description="deep", expect_text_contains="DEEP")],
            ),
        ],
    )
    ok, detail, recovered = engine._check_checkpoint(checkpoint, {}, {})
    assert ok is True


# ---- has_meaningful_replay_contract recognizes the new condition types ---

def _minimal_artifact(success_checkpoint: Checkpoint) -> CapabilityArtifact:
    return CapabilityArtifact(
        capability_id="x", name="x", description="x",
        target=ApplicationTarget(app_key="mockbank", base_url="http://x/"),
        steps=[
            Step(id="step_1", description="click", action=Action(
                type=ActionType.CLICK,
                target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Go")),
            )),
        ],
        success_checkpoint=success_checkpoint,
        created_from_run_id="run1",
    )


def test_gate_rejects_url_only_checkpoint():
    artifact = _minimal_artifact(Checkpoint(description="x", expect_url_contains="/x"))
    assert not has_meaningful_replay_contract(artifact)


def test_gate_accepts_element_visible_checkpoint():
    loc = Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Balance")
    artifact = _minimal_artifact(Checkpoint(description="x", expect_url_contains="/x", expect_element_visible=loc))
    assert has_meaningful_replay_contract(artifact)


def test_gate_accepts_output_present_checkpoint():
    artifact = _minimal_artifact(Checkpoint(description="x", expect_output_present="balance"))
    assert has_meaningful_replay_contract(artifact)


def test_gate_accepts_any_of_with_a_strong_alternative():
    artifact = _minimal_artifact(Checkpoint(
        description="x", expect_url_contains="/x",
        any_of=[Checkpoint(description="alt", expect_text_contains="Balance")],
    ))
    assert has_meaningful_replay_contract(artifact)


def test_gate_rejects_any_of_where_every_alternative_is_also_weak():
    artifact = _minimal_artifact(Checkpoint(
        description="x", expect_url_contains="/x",
        any_of=[Checkpoint(description="alt", expect_url_contains="/y")],
    ))
    assert not has_meaningful_replay_contract(artifact)


# ---- structural fingerprint differentiates on the new fields -------------

def test_fingerprint_differs_when_element_visible_differs():
    a = _minimal_artifact(Checkpoint(description="x", expect_element=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|A")))
    b = _minimal_artifact(Checkpoint(description="x", expect_element_visible=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|A")))
    assert _fingerprint(a) != _fingerprint(b)


def test_fingerprint_differs_when_output_present_differs():
    a = _minimal_artifact(Checkpoint(description="x", expect_output_present="balance"))
    b = _minimal_artifact(Checkpoint(description="x", expect_output_present="other"))
    assert _fingerprint(a) != _fingerprint(b)


def test_fingerprint_same_when_only_wording_differs_even_with_any_of():
    a = _minimal_artifact(Checkpoint(
        description="first wording",
        any_of=[Checkpoint(description="alt wording one", expect_text_contains="X")],
    ))
    b = _minimal_artifact(Checkpoint(
        description="totally different wording",
        any_of=[Checkpoint(description="alt wording two", expect_text_contains="X")],
    ))
    assert _fingerprint(a) == _fingerprint(b)
