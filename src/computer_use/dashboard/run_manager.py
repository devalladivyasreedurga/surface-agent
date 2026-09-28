"""Tracks discovery/replay runs the dashboard kicked off in background
threads, and derives a human-readable "what's happening right now" from the
run's own evidence log -- no separate progress-reporting channel needed,
since RunLogger already writes each event to log.jsonl as it happens.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel


class RunRecord(BaseModel):
    run_id: str
    kind: Literal["discovery", "replay"]
    status: Literal["running", "completed", "error"] = "running"
    started_at: datetime
    finished_at: datetime | None = None
    evidence_dir: str | None = None
    error: str | None = None

    # discovery
    goal: str | None = None
    target_url: str | None = None
    capability_id: str | None = None
    capability_version: int | None = None
    capability_name: str | None = None
    artifact_created: bool | None = None
    artifact_reason: str | None = None

    # replay
    inputs: dict[str, str] | None = None

    # shared final result payload (DiscoveryResult / ReplayResult, dumped)
    result: dict[str, Any] | None = None


class DashboardRunManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    def start(self, run_id: str, kind: str, **fields: Any) -> RunRecord:
        record = RunRecord(run_id=run_id, kind=kind, started_at=datetime.now(timezone.utc), **fields)
        with self._lock:
            self._runs[run_id] = record
        return record

    def complete(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            record.status = "completed"
            record.finished_at = datetime.now(timezone.utc)
            for k, v in fields.items():
                setattr(record, k, v)

    def fail(self, run_id: str, error: str) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            record.status = "error"
            record.finished_at = datetime.now(timezone.utc)
            record.error = error

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_recent(self, limit: int = 20) -> list[RunRecord]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)[:limit]


def _humanize_action(action: dict) -> str:
    action_type = action.get("type")
    target = (action.get("target") or {}).get("primary") or {}
    raw_value = target.get("value") or ""
    name = raw_value.split("|")[-1] if "|" in raw_value else raw_value
    if action_type == "navigate":
        return f"Navigating to {action.get('value')}"
    if action_type == "fill":
        return f"Filling '{name}'"
    if action_type == "click":
        return f"Clicking '{name}'"
    if action_type == "extract":
        return f"Extracting '{name}'"
    if action_type == "wait":
        return f"Waiting {action.get('value')}s"
    return action_type or "Working..."


def tail_progress(evidence_dir: Path | None) -> str:
    """Best-effort 'what is this run doing right now', derived by reading
    the last few lines of the run's own live evidence log. Returns a short
    display string; never raises (the log may not exist yet, or a line may
    be mid-write)."""
    if evidence_dir is None:
        return "Starting..."
    log_path = evidence_dir / "log.jsonl"
    if not log_path.exists():
        return "Starting..."
    try:
        lines = log_path.read_text().strip().splitlines()
    except Exception:
        return "Working..."
    for line in reversed(lines[-15:]):
        try:
            event = json.loads(line)
        except Exception:
            continue
        ev = event.get("event")
        if ev == "action_executed":
            return _humanize_action(event.get("action") or {})
        if ev == "escalation_raised":
            return f"Paused: waiting for human confirmation ({event.get('reason')})"
        if ev == "escalation_resumed":
            return "Resumed by operator, continuing..."
        if ev == "initial_navigation":
            return f"Navigating to {event.get('url')}"
        if ev == "replay_business_outcome":
            return f"Business outcome: {event.get('code')}"
        if ev == "replay_hard_failure":
            return f"Hard failure at step {event.get('step')}"
        if ev == "discovery_finished":
            return "Finishing up..."
    return "Working..."
