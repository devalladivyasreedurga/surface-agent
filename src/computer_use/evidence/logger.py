"""Structured, redacted evidence for a single discovery or replay run.

Every event and saved JSON blob passes through the redaction layer before it
touches disk. Screenshots are referenced by path, not redacted at the pixel
level (see safety/redaction.py for why, and REPORT.md Cuts for the follow-up).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from computer_use.safety.redaction import redact_dict


class RunLogger:
    def __init__(self, run_id: str, kind: str, evidence_root: Path):
        self.run_id = run_id
        self.kind = kind
        self.run_dir = evidence_root / f"{kind}_{run_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.run_dir / "log.jsonl"

    def log(self, event_type: str, **fields: Any) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **fields,
        }
        entry = redact_dict(entry)
        with self._log_path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def save_json(self, name: str, obj: dict) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(redact_dict(obj), indent=2, default=str))
        return path

    def screenshot_dir(self) -> Path:
        d = self.run_dir / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        return d
