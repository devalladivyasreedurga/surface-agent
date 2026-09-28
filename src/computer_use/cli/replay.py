#!/usr/bin/env python3
"""Deterministically replay a saved capability artifact. Makes zero LLM calls.

Example:
    python -m computer_use.cli.replay \\
        --artifact artifacts/lookup_member_savings_balance.v1.json \\
        --target http://localhost:8000 \\
        --input member_id=10002
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from computer_use import services
from computer_use.models.artifact import CapabilityArtifact
from computer_use.models.results import ReplayStatus


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--artifact", required=True, help="Path to a saved capability artifact JSON file.")
    p.add_argument("--target", default=None, help="Override the artifact's recorded base URL (e.g. a different tenant).")
    p.add_argument("--input", action="append", default=[], metavar="key=value", help="Repeatable. Runtime input parameter.")
    p.add_argument("--policy", default="config/allowlist.json")
    p.add_argument("--evidence-dir", default="evidence")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--operator-port", type=int, default=8001)
    return p


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--input must be key=value, got: {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_arg_parser().parse_args(argv)

    artifact = CapabilityArtifact.model_validate_json(Path(args.artifact).read_text())
    inputs = _parse_inputs(args.input)

    interventions, operator_base_url = services.ensure_operator_console(args.operator_port)
    print(f"[operator console] {operator_base_url}")

    outcome = services.replay_capability(
        artifact=artifact,
        inputs=inputs,
        interventions=interventions,
        operator_base_url=operator_base_url,
        target_url_override=args.target,
        policy_path=Path(args.policy),
        evidence_dir=Path(args.evidence_dir),
        headless=args.headless,
    )
    result = outcome.result

    print(f"\nReplay finished: {result.status.value} ({result.steps_completed}/{result.steps_total} steps).")
    if result.status == ReplayStatus.SUCCESS:
        print(f"Outputs: {json.dumps(result.outputs, indent=2)}")
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        print(f"Business outcome: {result.business_outcome.code} -- {result.business_outcome.message}")
    elif result.status == ReplayStatus.HARD_FAILURE:
        hf = result.hard_failure
        print(f"Hard failure at step '{hf.step_id}': expected {hf.expected!r}, observed {hf.observed!r}")
        print(f"Error code: {hf.error_code} -- {hf.message}")
        if hf.evidence_ref:
            print(f"Evidence: {hf.evidence_ref}")
    if result.recovery_events:
        print(f"Recovery events: {[e.model_dump() for e in result.recovery_events]}")
    print(f"Evidence written to: {outcome.evidence_dir}")

    return 0 if result.status != ReplayStatus.HARD_FAILURE else 2


if __name__ == "__main__":
    sys.exit(main())
