"""Deterministic, configurable guardrails. Runs before every Surface.act, in
both discovery and replay.

Deliberate design choice: risk classification here is a keyword/pattern scan
run independently of whatever risk label an artifact's Step already carries.
The artifact's stored `risk` is trusted input (it was set at record time,
possibly by an LLM), so treating it as the *only* signal would let a
mis-tagged or adversarially-crafted artifact bypass guardrails. The policy
engine, not the model, is the final authority -- exactly what the assignment
asks for.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from computer_use.models.actions import Action, ActionType
from computer_use.models.artifact import RiskLevel

DEFAULT_RISKY_KEYWORDS = [
    "transfer", "wire", "withdraw", "close account", "delete", "remove account",
    "submit payment", "pay now", "confirm payment", "authorize", "send money",
    "move funds", "charge card",
]


class PolicyConfig(BaseModel):
    allowed_domains: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    allowed_routes: list[str] = Field(
        default_factory=list, description="Regex patterns on URL path. Empty = all paths on allowed domains."
    )
    allowed_action_types: list[ActionType] = Field(default_factory=lambda: list(ActionType))
    risky_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_RISKY_KEYWORDS))
    irreversible_action_policy: str = Field(
        default="require_confirmation", description="'block' or 'require_confirmation'"
    )

    @classmethod
    def load(cls, path: Path) -> "PolicyConfig":
        return cls.model_validate(json.loads(path.read_text()))


class PolicyDecision(BaseModel):
    allowed: bool
    requires_confirmation: bool = False
    reason: str
    matched_risk: RiskLevel = RiskLevel.SAFE


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self._config = config
        self._keyword_re = re.compile(
            "|".join(re.escape(k) for k in config.risky_keywords), re.IGNORECASE
        ) if config.risky_keywords else None

    def evaluate(self, action: Action, *, current_url: str, step_risk: RiskLevel = RiskLevel.SAFE) -> PolicyDecision:
        if action.type not in self._config.allowed_action_types:
            return PolicyDecision(allowed=False, reason=f"action type '{action.type.value}' is not in allowlist")

        url_to_check = action.value if action.type == ActionType.NAVIGATE and action.value else current_url
        domain_decision = self._check_domain(url_to_check)
        if domain_decision is not None:
            return domain_decision

        risk = self._classify_risk(action, step_risk)
        if risk == RiskLevel.IRREVERSIBLE:
            if self._config.irreversible_action_policy == "block":
                return PolicyDecision(
                    allowed=False, reason="irreversible action blocked by policy", matched_risk=risk
                )
            return PolicyDecision(
                allowed=True,
                requires_confirmation=True,
                reason="irreversible action requires human confirmation before execution",
                matched_risk=risk,
            )

        return PolicyDecision(allowed=True, reason="ok", matched_risk=risk)

    def _check_domain(self, url: str) -> PolicyDecision | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return PolicyDecision(allowed=False, reason=f"could not parse url '{url}'")
        host = parsed.hostname or ""
        if host and host not in self._config.allowed_domains:
            return PolicyDecision(allowed=False, reason=f"domain '{host}' is not in allowlist")
        if self._config.allowed_routes:
            if not any(re.search(p, parsed.path or "/") for p in self._config.allowed_routes):
                return PolicyDecision(allowed=False, reason=f"route '{parsed.path}' is not in allowlist")
        return None

    def _classify_risk(self, action: Action, step_risk: RiskLevel) -> RiskLevel:
        haystack_parts = [action.value or "", action.reasoning or ""]
        if action.target is not None:
            for loc in action.target.ranked():
                haystack_parts.append(loc.value)
                if loc.note:
                    haystack_parts.append(loc.note)
        haystack = " ".join(haystack_parts)

        keyword_hit = bool(self._keyword_re.search(haystack)) if self._keyword_re else False
        if keyword_hit or step_risk == RiskLevel.IRREVERSIBLE:
            return RiskLevel.IRREVERSIBLE
        return step_risk
