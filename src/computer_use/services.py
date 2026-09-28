"""Shared application services used by both the CLI entry points and the
dashboard. Neither discover.py/replay.py nor the dashboard construct a
DiscoveryAgent/ReplayEngine directly -- they both call these two functions,
so there is exactly one place that wires Surface + PolicyEngine +
Interventions + Logger + (discovery only) the LLM client + ArtifactRecorder +
CapabilityRegistry together. Keeping this in one module is what "UI and CLI
call the same underlying services, not duplicated logic" means concretely.

Ownership note: callers own the `InterventionManager` and its operator
console, not this module. A CLI invocation is a one-shot process that starts
a fresh operator console per run (see `ensure_operator_console` below); the
long-running dashboard starts exactly one at startup and passes the same
instance into every discovery/replay call it makes, so a human resuming an
intervention always lands on the one console that's actually watching the
run in question.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from computer_use.agent.artifact_recorder import ArtifactRecorder, has_meaningful_replay_contract, sanitize_slug
from computer_use.agent.capability_registry import CapabilityRegistry
from computer_use.agent.discovery_agent import DiscoveryAgent
from computer_use.escalation.intervention import InterventionManager
from computer_use.escalation.operator_app import build_operator_app, start_operator_server_in_background
from computer_use.evidence.logger import RunLogger
from computer_use.llm.anthropic_client import AnthropicLLMClient
from computer_use.llm.base import LLMClient
from computer_use.models.artifact import CapabilityArtifact
from computer_use.models.results import DiscoveryResult, DiscoveryStopReason, ReplayResult
from computer_use.models.target_spec import TargetSpec
from computer_use.replay.error_taxonomy import detect_business_outcome
from computer_use.replay.replay_engine import ReplayEngine
from computer_use.safety.policy import PolicyConfig, PolicyEngine
from computer_use.surface.playwright_surface import PlaywrightSurface


def ensure_operator_console(port: int) -> tuple[InterventionManager, str]:
    """Starts a fresh operator console in a background thread. Used by
    one-shot CLI invocations; the dashboard calls build_operator_app /
    start_operator_server_in_background itself once at startup instead, so
    it can keep one InterventionManager alive across many runs."""
    interventions = InterventionManager()
    app = build_operator_app(interventions)
    start_operator_server_in_background(app, "127.0.0.1", port)
    return interventions, f"http://127.0.0.1:{port}"


def _policy_for(target_url: str, policy_path: Path) -> PolicyEngine:
    policy_config = PolicyConfig.load(policy_path) if policy_path.exists() else PolicyConfig()
    host = urlparse(target_url).hostname
    if host and host not in policy_config.allowed_domains:
        policy_config.allowed_domains.append(host)
    return PolicyEngine(policy_config)


@dataclass
class DiscoveryOutcome:
    result: DiscoveryResult
    evidence_dir: Path
    artifact: CapabilityArtifact | None
    artifact_created: bool
    artifact_reason: str | None


def discover_and_record(
    *,
    target_url: str,
    goal: str,
    interventions: InterventionManager,
    operator_base_url: str,
    app_key: str = "mockbank",
    policy_path: Path = Path("config/allowlist.json"),
    evidence_dir: Path = Path("evidence"),
    artifacts_dir: Path = Path("artifacts"),
    max_steps: int = 20,
    timeout_seconds: float = 300.0,
    headless: bool = False,
    capability_id_override: str | None = None,
    capability_name_override: str | None = None,
    capability_description_override: str | None = None,
    run_id: str | None = None,
    llm: LLMClient | None = None,
) -> DiscoveryOutcome:
    target = TargetSpec(type="web", url=target_url)
    policy = _policy_for(target_url, policy_path)

    run_id = run_id or uuid.uuid4().hex[:10]
    logger = RunLogger(run_id=run_id, kind="discovery", evidence_root=evidence_dir)

    surface = PlaywrightSurface(headless=headless, evidence_dir=logger.screenshot_dir())
    llm = llm or AnthropicLLMClient()

    agent = DiscoveryAgent(
        llm=llm,
        surface=surface,
        policy=policy,
        interventions=interventions,
        logger=logger,
        operator_base_url=operator_base_url,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
    )
    try:
        result, traces, final_state = agent.run(goal=goal, target=target)
    finally:
        surface.close()

    artifact: CapabilityArtifact | None = None
    artifact_created = False
    artifact_reason: str | None = None

    if result.stop_reason == DiscoveryStopReason.SUCCESS and traces:
        capability_id = (
            capability_id_override
            or (sanitize_slug(result.capability_slug) if result.capability_slug else None)
            or (sanitize_slug(result.capability_name) if result.capability_name else None)
            or sanitize_slug(goal)
        )
        capability_name = capability_name_override or result.capability_name or goal
        capability_description = (
            capability_description_override
            or result.capability_description
            or f"Recorded capability for goal: {goal}"
        )

        candidate = ArtifactRecorder().record(
            run_id=result.run_id,
            goal=goal,
            target=target,
            traces=traces,
            final_state=final_state,
            capability_id=capability_id,
            capability_name=capability_name,
            capability_description=capability_description,
            app_key=app_key,
        )

        if not has_meaningful_replay_contract(candidate):
            # The run succeeded at its goal, but never observed a state that
            # would let replay tell success apart from a known business
            # outcome (see artifact_recorder.has_meaningful_replay_contract).
            # Keep the run as evidence/provenance; do not persist or version
            # a capability that could only ever report a hollow "success".
            business = detect_business_outcome((final_state.visible_text_excerpt if final_state else "") or "")
            if business:
                code, _message = business
                artifact_reason = (
                    f"Discovery completed with business outcome {code}; no reusable capability was "
                    "recorded because the successful execution path was not observed."
                )
            else:
                artifact_reason = (
                    "Discovery completed, but no reusable capability was recorded: the run had no "
                    "extraction step and no success checkpoint stronger than a bare URL match, so "
                    "replay could never distinguish success from a known business outcome."
                )
            logger.log(
                "no_capability_recorded",
                capability_id_considered=candidate.capability_id,
                reason=artifact_reason,
                business_outcome_code=business[0] if business else None,
            )
        else:
            registry = CapabilityRegistry(artifacts_dir)
            artifact, artifact_created, artifact_reason = registry.reconcile(candidate, run_id=result.run_id)
            logger.save_json("artifact.json", artifact.model_dump(mode="json"))
            logger.log(
                "artifact_reconciled",
                capability_id=artifact.capability_id,
                capability_version=artifact.capability_version,
                created=artifact_created,
                reason=artifact_reason,
            )

    return DiscoveryOutcome(
        result=result,
        evidence_dir=logger.run_dir,
        artifact=artifact,
        artifact_created=artifact_created,
        artifact_reason=artifact_reason,
    )


@dataclass
class ReplayOutcome:
    result: ReplayResult
    evidence_dir: Path


def replay_capability(
    *,
    artifact: CapabilityArtifact,
    inputs: dict[str, str],
    interventions: InterventionManager,
    operator_base_url: str,
    target_url_override: str | None = None,
    policy_path: Path = Path("config/allowlist.json"),
    evidence_dir: Path = Path("evidence"),
    headless: bool = False,
    run_id: str | None = None,
) -> ReplayOutcome:
    effective_url = target_url_override or artifact.target.base_url
    policy = _policy_for(effective_url, policy_path)

    run_id = run_id or uuid.uuid4().hex[:10]
    logger = RunLogger(run_id=run_id, kind="replay", evidence_root=evidence_dir)

    surface = PlaywrightSurface(headless=headless, evidence_dir=logger.screenshot_dir())
    engine = ReplayEngine(
        surface=surface,
        policy=policy,
        interventions=interventions,
        logger=logger,
        operator_base_url=operator_base_url,
    )
    try:
        result = engine.run(artifact=artifact, inputs=inputs, base_url_override=target_url_override)
    finally:
        surface.close()

    return ReplayOutcome(result=result, evidence_dir=logger.run_dir)
