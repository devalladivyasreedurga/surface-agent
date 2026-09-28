from computer_use.models.actions import Action, ActionType, Locator, LocatorStrategy, Target
from computer_use.models.artifact import RiskLevel
from computer_use.safety.policy import PolicyConfig, PolicyEngine


def _engine(**overrides) -> PolicyEngine:
    config = PolicyConfig(allowed_domains=["localhost", "127.0.0.1"], **overrides)
    return PolicyEngine(config)


def test_allows_safe_action_on_allowed_domain():
    engine = _engine()
    action = Action(type=ActionType.NAVIGATE, value="http://127.0.0.1:8000/members/10001")
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.SAFE)
    assert decision.allowed
    assert not decision.requires_confirmation


def test_blocks_disallowed_domain():
    engine = _engine()
    action = Action(type=ActionType.NAVIGATE, value="http://evil.example.com/")
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.SAFE)
    assert not decision.allowed
    assert "domain" in decision.reason


def test_blocks_disallowed_action_type():
    engine = _engine(allowed_action_types=[ActionType.NAVIGATE, ActionType.CLICK])
    action = Action(type=ActionType.FILL, value="hello", target=Target(
        primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="textbox|Note")
    ))
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.SAFE)
    assert not decision.allowed


def test_risky_keyword_requires_confirmation_by_default():
    engine = _engine()
    action = Action(
        type=ActionType.CLICK,
        target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Transfer Funds")),
        reasoning="user asked to transfer funds between accounts",
    )
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.SAFE)
    assert decision.allowed
    assert decision.requires_confirmation
    assert decision.matched_risk == RiskLevel.IRREVERSIBLE


def test_risky_action_blocked_when_policy_is_block():
    engine = _engine(irreversible_action_policy="block")
    action = Action(
        type=ActionType.CLICK,
        target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="button|Wire Transfer")),
    )
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.SAFE)
    assert not decision.allowed
    assert decision.matched_risk == RiskLevel.IRREVERSIBLE


def test_artifact_declared_risk_cannot_downgrade_a_keyword_hit():
    """An artifact step tagged 'safe' whose action text still matches a risky
    keyword must still be classified irreversible -- the artifact's own risk
    label is not the final authority (see REPORT.md, Safety)."""
    engine = _engine()
    action = Action(type=ActionType.CLICK, reasoning="submit payment to close out the balance")
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.SAFE)
    assert decision.requires_confirmation
    assert decision.matched_risk == RiskLevel.IRREVERSIBLE


def test_step_declared_irreversible_risk_forces_confirmation_even_without_keyword():
    engine = _engine()
    action = Action(
        type=ActionType.EXTRACT,
        target=Target(primary=Locator(strategy=LocatorStrategy.ACCESSIBILITY, value="cell|Checking Balance Value")),
        value="checking_balance",
    )
    decision = engine.evaluate(action, current_url="http://127.0.0.1:8000/", step_risk=RiskLevel.IRREVERSIBLE)
    assert decision.requires_confirmation
