#!/usr/bin/env python3
"""Run an LLM-driven discovery agent against a live target. On success, the
run is turned into a canonical capability artifact and reconciled against
`artifacts/` -- a structurally identical existing capability is reused
(and this run recorded as provenance only), a changed flow gets a new
version, and a genuinely new capability gets v1.

Example:
    python -m computer_use.cli.discover \\
        --target http://localhost:8000 \\
        --goal "Look up member 10001 and read the savings balance"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from computer_use import services
from computer_use.models.results import DiscoveryStopReason


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", required=True, help="Base URL of the target application.")
    p.add_argument("--goal", required=True, help="Natural language goal for the agent.")
    p.add_argument("--capability-id", default=None, help="Override the canonical capability id.")
    p.add_argument("--capability-name", default=None, help="Override the generated capability name.")
    p.add_argument("--capability-description", default=None, help="Override the generated capability description.")
    p.add_argument("--app-key", default="mockbank", help="Vendor/product identity for this target.")
    p.add_argument("--policy", default="config/allowlist.json")
    p.add_argument("--evidence-dir", default="evidence")
    p.add_argument("--artifacts-dir", default="artifacts")
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--headless", action="store_true", help="Run the browser headless (default: headful).")
    p.add_argument("--operator-port", type=int, default=8001)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_arg_parser().parse_args(argv)

    interventions, operator_base_url = services.ensure_operator_console(args.operator_port)
    print(f"[operator console] {operator_base_url}")

    outcome = services.discover_and_record(
        target_url=args.target,
        goal=args.goal,
        interventions=interventions,
        operator_base_url=operator_base_url,
        app_key=args.app_key,
        policy_path=Path(args.policy),
        evidence_dir=Path(args.evidence_dir),
        artifacts_dir=Path(args.artifacts_dir),
        max_steps=args.max_steps,
        timeout_seconds=args.timeout,
        headless=args.headless,
        capability_id_override=args.capability_id,
        capability_name_override=args.capability_name,
        capability_description_override=args.capability_description,
    )

    result = outcome.result
    print(f"\nDiscovery finished: {result.stop_reason.value} after {result.steps_taken} step(s).")
    if result.summary:
        print(f"Summary: {result.summary}")
    if result.outputs:
        print(f"Outputs found this run: {json.dumps(result.outputs, indent=2)}")
    print(f"Evidence written to: {outcome.evidence_dir}")

    if outcome.artifact is None:
        if result.stop_reason != DiscoveryStopReason.SUCCESS:
            print("No artifact recorded (run did not reach success).")
        else:
            print(outcome.artifact_reason or "No reusable capability was recorded.")
        return 1

    artifact = outcome.artifact
    verb = "Created" if outcome.artifact_created else "Reused existing"
    print(f"{verb} capability: {artifact.capability_id}.v{artifact.capability_version} ({outcome.artifact_reason})")
    print(f"Capability name: {artifact.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
