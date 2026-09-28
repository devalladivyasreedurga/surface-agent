#!/usr/bin/env python3
"""Start the main automation dashboard (Discover / Capabilities-Replay /
Interventions). Starts its own operator console in the background too.

Example:
    python -m computer_use.cli.dashboard
    python -m computer_use.cli.dashboard --port 8002 --operator-port 8001
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from computer_use.dashboard.app import build_app


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8002)
    p.add_argument("--operator-port", type=int, default=8001)
    p.add_argument("--default-target", default="http://127.0.0.1:8000")
    p.add_argument("--artifacts-dir", default="artifacts")
    p.add_argument("--evidence-dir", default="evidence")
    p.add_argument("--policy", default="config/allowlist.json")
    p.add_argument("--headless", action="store_true", help="Run discovery/replay browsers headless by default.")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_arg_parser().parse_args(argv)

    app = build_app(
        artifacts_dir=Path(args.artifacts_dir),
        evidence_dir=Path(args.evidence_dir),
        policy_path=Path(args.policy),
        default_target_url=args.default_target,
        default_headless=args.headless,
        operator_port=args.operator_port,
    )

    print(f"[dashboard] http://{args.host}:{args.port}")
    print(f"[operator console] http://127.0.0.1:{args.operator_port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
