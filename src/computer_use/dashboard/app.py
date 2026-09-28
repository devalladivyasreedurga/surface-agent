"""Minimal user-facing dashboard: Discover, Capabilities/Replay, Interventions.

This is the control/observability surface -- it never embeds the target
application. The actual browser Playwright drives is a separate, real,
visible window; this UI only shows status text derived from that run's
evidence log (see run_manager.tail_progress) and lets you kick off new runs.
Both this dashboard and the CLI (discover.py/replay.py) call the exact same
functions in computer_use.services -- nothing here re-implements discovery
or replay logic.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from computer_use import services
from computer_use.agent.capability_registry import CapabilityRegistry
from computer_use.dashboard.run_manager import DashboardRunManager, tail_progress
from computer_use.escalation.intervention import InterventionManager, InterventionStatus
from computer_use.escalation.operator_app import build_operator_app, start_operator_server_in_background
from computer_use.models.artifact import CapabilityArtifact

_STYLE = """
<style>
body { font-family: system-ui, sans-serif; margin: 0; color: #1a1a1a; background: #fafafa; }
nav { background: #003366; padding: 0.75rem 1.5rem; }
nav a { color: white; text-decoration: none; margin-right: 1.5rem; font-weight: 600; }
nav a:hover { text-decoration: underline; }
main { max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
.card { border: 1px solid #ccc; border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1rem; background: white; }
label { display: block; font-weight: 600; margin-top: 0.75rem; }
input[type=text] { width: 100%; padding: 0.4rem; margin-top: 0.25rem; box-sizing: border-box; }
button { margin-top: 1rem; padding: 0.5rem 1.25rem; background: #003366; color: white; border: none; border-radius: 4px; cursor: pointer; }
button:hover { background: #00509e; }
.status-running { color: #b36b00; font-weight: 600; }
.status-completed { color: #2a7a2a; font-weight: 600; }
.status-error { color: #b33; font-weight: 600; }
.kv { margin: 0.25rem 0; }
.kv b { display: inline-block; min-width: 140px; }
ul.capabilities { list-style: none; padding: 0; }
ul.capabilities li { padding: 0.75rem; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 0.5rem; }
ul.capabilities a { font-weight: 600; color: #003366; }
code { background: #eee; padding: 1px 5px; border-radius: 3px; }
</style>
"""


def _layout(title: str, body: str, refresh_seconds: int | None = None) -> HTMLResponse:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    html = f"""<html><head><title>{title}</title>{refresh_tag}{_STYLE}</head><body>
<nav>
  <a href="/discover">Discover</a>
  <a href="/capabilities">Capabilities / Replay</a>
  <a href="/interventions">Interventions</a>
</nav>
<main>{body}</main>
</body></html>"""
    return HTMLResponse(html)


def build_app(
    *,
    artifacts_dir: Path = Path("artifacts"),
    evidence_dir: Path = Path("evidence"),
    policy_path: Path = Path("config/allowlist.json"),
    default_target_url: str = "http://127.0.0.1:8000",
    default_headless: bool = False,
    operator_port: int = 8001,
) -> FastAPI:
    app = FastAPI(title="Computer-Use Automation Dashboard")

    interventions = InterventionManager()
    operator_app = build_operator_app(interventions)
    start_operator_server_in_background(operator_app, "127.0.0.1", operator_port)
    operator_base_url = f"http://127.0.0.1:{operator_port}"

    runs = DashboardRunManager()
    registry = CapabilityRegistry(artifacts_dir)

    # ---- Discover ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse(url="/discover")

    @app.get("/discover", response_class=HTMLResponse)
    def discover_form():
        body = f"""
        <h1>Discover a capability</h1>
        <div class="card">
          <form method="post" action="/discover">
            <label for="target">Target URL</label>
            <input type="text" id="target" name="target" value="{default_target_url}">
            <label for="goal">Goal</label>
            <input type="text" id="goal" name="goal" placeholder="Look up member 1008 and read the savings balance">
            <button type="submit">Run Discovery</button>
          </form>
        </div>
        <p>Recent runs:</p>
        {_recent_runs_list(runs, kind="discovery")}
        """
        return _layout("Discover", body)

    @app.post("/discover")
    def discover_submit(target: str = Form(...), goal: str = Form(...)):
        run_id = uuid.uuid4().hex[:10]
        runs.start(run_id, "discovery", goal=goal, target_url=target, evidence_dir=None)
        thread = threading.Thread(
            target=_run_discovery, args=(run_id, target, goal, runs, interventions, operator_base_url,
                                          artifacts_dir, evidence_dir, policy_path, default_headless),
            daemon=True,
        )
        thread.start()
        return RedirectResponse(url=f"/discover/{run_id}", status_code=303)

    @app.get("/discover/{run_id}", response_class=HTMLResponse)
    def discover_status(run_id: str):
        record = runs.get(run_id)
        if record is None:
            return _layout("Not found", "<p>No such run.</p>")

        evidence_path = Path(record.evidence_dir) if record.evidence_dir else None
        body = [f"<h1>Discovery run {run_id}</h1>"]
        body.append(f'<div class="card">')
        body.append(f'<p class="kv"><b>Goal</b> {record.goal}</p>')
        body.append(f'<p class="kv"><b>Target</b> {record.target_url}</p>')

        if record.status == "running":
            body.append('<p class="kv"><b>Status</b> <span class="status-running">Running</span></p>')
            body.append(f'<p class="kv"><b>Step</b> {tail_progress(evidence_path)}</p>')
        elif record.status == "error":
            body.append('<p class="kv"><b>Status</b> <span class="status-error">Error</span></p>')
            body.append(f'<p class="kv"><b>Detail</b> {record.error}</p>')
        else:
            r = record.result or {}
            body.append('<p class="kv"><b>Status</b> <span class="status-completed">Discovery completed</span></p>')
            body.append(f'<p class="kv"><b>Result</b> {r.get("stop_reason", "?")}</p>')
            if r.get("summary"):
                body.append(f'<p class="kv"><b>Summary</b> {r.get("summary")}</p>')
            if r.get("outputs"):
                body.append(f'<p class="kv"><b>Output</b> {_format_outputs(r.get("outputs"))}</p>')
            if record.capability_id:
                verb = "Created" if record.artifact_created else "Reused existing"
                body.append(f'<p class="kv"><b>Capability</b> {record.capability_name}</p>')
                body.append(
                    f'<p class="kv"><b>Artifact</b> {record.capability_id}.v{record.capability_version} '
                    f"({verb.lower()} &mdash; {record.artifact_reason})</p>"
                )
            else:
                reason = record.artifact_reason or "none (run did not reach success)"
                body.append(f'<p class="kv"><b>Capability</b> none &mdash; {reason}</p>')

        if record.evidence_dir:
            body.append(f'<p class="kv"><b>Evidence</b> <code>{record.evidence_dir}</code></p>')
        body.append("</div>")

        refresh = 2 if record.status == "running" else None
        return _layout(f"Discovery {run_id}", "".join(body), refresh_seconds=refresh)

    # ---- Capabilities / Replay ------------------------------------------

    @app.get("/capabilities", response_class=HTMLResponse)
    def capabilities_list():
        caps = registry.list_capabilities()
        if not caps:
            items = "<p>No capabilities recorded yet. Run a discovery first.</p>"
        else:
            rows = []
            for c in caps:
                inputs = ", ".join(p.name for p in c.inputs) or "(none)"
                outputs = ", ".join(o.name for o in c.outputs) or "(none)"
                rows.append(
                    f'<li><a href="/capabilities/{c.capability_id}/replay">{c.name}</a> '
                    f'<span>(v{c.capability_version})</span><br>'
                    f'<small>{c.description}</small><br>'
                    f'<small>Input: {inputs} &nbsp;|&nbsp; Output: {outputs}</small></li>'
                )
            items = f'<ul class="capabilities">{"".join(rows)}</ul>'
        body = f"<h1>Saved Capabilities</h1>{items}"
        return _layout("Capabilities", body)

    @app.get("/capabilities/{capability_id}/replay", response_class=HTMLResponse)
    def replay_form(capability_id: str):
        artifact = registry.get(capability_id)
        if artifact is None:
            return _layout("Not found", "<p>No such capability.</p>")
        input_fields = "".join(
            f'<label for="input_{p.name}">{p.name}{" *" if p.required else ""}</label>'
            f'<input type="text" id="input_{p.name}" name="input_{p.name}" placeholder="{p.example or ""}">'
            for p in artifact.inputs
        )
        body = f"""
        <h1>Replay: {artifact.name}</h1>
        <div class="card">
          <p>{artifact.description}</p>
          <form method="post" action="/capabilities/{capability_id}/replay">
            <label for="target">Target</label>
            <input type="text" id="target" name="target" value="{artifact.target.base_url}">
            {input_fields}
            <button type="submit">Run Replay</button>
          </form>
        </div>
        <p>Recent replays of this capability:</p>
        {_recent_runs_list(runs, kind="replay", capability_id=capability_id)}
        """
        return _layout(f"Replay {artifact.name}", body)

    @app.post("/capabilities/{capability_id}/replay")
    async def replay_submit(capability_id: str, request: Request):
        # A capability's inputs are dynamic (one artifact might need
        # member_id, another member_id+amount), so field names on this form
        # aren't known ahead of time -- read the raw form instead of
        # declaring each one as a typed Form(...) parameter.
        form = await request.form()
        target = form.get("target") or default_target_url
        inputs = {
            key[len("input_"):]: value
            for key, value in form.items()
            if key.startswith("input_") and value
        }
        artifact = registry.get(capability_id)
        if artifact is None:
            return _layout("Not found", "<p>No such capability.</p>")

        run_id = uuid.uuid4().hex[:10]
        runs.start(run_id, "replay", capability_id=capability_id, capability_name=artifact.name,
                   target_url=target, inputs=inputs, evidence_dir=None)
        thread = threading.Thread(
            target=_run_replay, args=(run_id, artifact, inputs, target, runs, interventions, operator_base_url,
                                       evidence_dir, policy_path, default_headless),
            daemon=True,
        )
        thread.start()
        return RedirectResponse(url=f"/replay/{run_id}", status_code=303)

    @app.get("/replay/{run_id}", response_class=HTMLResponse)
    def replay_status(run_id: str):
        record = runs.get(run_id)
        if record is None:
            return _layout("Not found", "<p>No such run.</p>")

        evidence_path = Path(record.evidence_dir) if record.evidence_dir else None
        body = [f"<h1>Replay run {run_id}</h1>"]
        body.append('<div class="card">')
        body.append(f'<p class="kv"><b>Capability</b> {record.capability_name} ({record.capability_id})</p>')
        body.append(f'<p class="kv"><b>Target</b> {record.target_url}</p>')
        body.append(f'<p class="kv"><b>Inputs</b> {record.inputs}</p>')

        if record.status == "running":
            body.append('<p class="kv"><b>Status</b> <span class="status-running">Running</span></p>')
            body.append(f'<p class="kv"><b>Step</b> {tail_progress(evidence_path)}</p>')
        elif record.status == "error":
            body.append('<p class="kv"><b>Status</b> <span class="status-error">Error</span></p>')
            body.append(f'<p class="kv"><b>Detail</b> {record.error}</p>')
        else:
            r = record.result or {}
            status = r.get("status")
            css = "status-completed" if status == "success" else "status-error" if status == "hard_failure" else "status-completed"
            body.append(f'<p class="kv"><b>Status</b> <span class="{css}">{status}</span></p>')
            if status == "success":
                body.append(f'<p class="kv"><b>Outputs</b> {r.get("outputs")}</p>')
            elif status == "business_outcome":
                bo = r.get("business_outcome") or {}
                body.append(f'<p class="kv"><b>Business outcome</b> {bo.get("code")} &mdash; {bo.get("message")}</p>')
            elif status == "hard_failure":
                hf = r.get("hard_failure") or {}
                body.append(f'<p class="kv"><b>Hard failure</b> step {hf.get("step_id")}: {hf.get("message")}</p>')
            if r.get("recovery_events"):
                body.append(f'<p class="kv"><b>Recovery events</b> {r.get("recovery_events")}</p>')

        if record.evidence_dir:
            body.append(f'<p class="kv"><b>Evidence</b> <code>{record.evidence_dir}</code></p>')
        body.append("</div>")

        refresh = 2 if record.status == "running" else None
        return _layout(f"Replay {run_id}", "".join(body), refresh_seconds=refresh)

    # ---- Interventions ---------------------------------------------------

    @app.get("/interventions", response_class=HTMLResponse)
    def interventions_page():
        pending = [r for r in interventions.list_all() if r.status == InterventionStatus.PENDING]
        resolved = [r for r in interventions.list_all() if r.status != InterventionStatus.PENDING]
        rows = "".join(
            f'<li><a href="{operator_base_url}/interventions/{r.id}" target="_blank">{r.reason.value}</a> '
            f"&mdash; run {r.run_id} &mdash; {r.reason_detail}</li>"
            for r in pending
        ) or "<p>No pending interventions.</p>"
        resolved_rows = "".join(
            f"<li>{r.reason.value} &mdash; run {r.run_id} &mdash; resolved</li>" for r in resolved[:10]
        )
        body = f"""
        <h1>Interventions</h1>
        <p>Human handoff uses the operator console directly (same live browser
        session as the run) at <a href="{operator_base_url}" target="_blank">{operator_base_url}</a>.</p>
        <div class="card"><h3>Pending</h3><ul>{rows}</ul></div>
        <div class="card"><h3>Recently resolved</h3><ul>{resolved_rows or "<p>None yet.</p>"}</ul></div>
        """
        return _layout("Interventions", body, refresh_seconds=5)

    return app


def _format_outputs(outputs: dict) -> str:
    return "; ".join(f"{k} = {v}" for k, v in outputs.items())


def _recent_runs_list(runs: DashboardRunManager, *, kind: str, capability_id: str | None = None) -> str:
    records = [r for r in runs.list_recent(50) if r.kind == kind]
    if capability_id:
        records = [r for r in records if r.capability_id == capability_id]
    records = records[:5]
    if not records:
        return "<p>(none yet)</p>"
    path_prefix = "/discover/" if kind == "discovery" else "/replay/"
    rows = "".join(
        f'<li><a href="{path_prefix}{r.run_id}">{r.run_id}</a> &mdash; {r.status}</li>' for r in records
    )
    return f"<ul>{rows}</ul>"


def _run_discovery(run_id, target, goal, runs, interventions, operator_base_url,
                    artifacts_dir, evidence_dir, policy_path, headless):
    try:
        outcome = services.discover_and_record(
            target_url=target,
            goal=goal,
            interventions=interventions,
            operator_base_url=operator_base_url,
            policy_path=policy_path,
            evidence_dir=evidence_dir,
            artifacts_dir=artifacts_dir,
            headless=headless,
            run_id=run_id,
        )
        record = runs.get(run_id)
        if record is not None:
            record.evidence_dir = str(outcome.evidence_dir)
        fields = dict(result=outcome.result.model_dump(mode="json"), artifact_reason=outcome.artifact_reason)
        if outcome.artifact is not None:
            fields.update(
                capability_id=outcome.artifact.capability_id,
                capability_version=outcome.artifact.capability_version,
                capability_name=outcome.artifact.name,
                artifact_created=outcome.artifact_created,
            )
        runs.complete(run_id, **fields)
    except Exception as e:  # noqa: BLE001 -- surfaced to the dashboard, not swallowed
        runs.fail(run_id, str(e))


def _run_replay(run_id, artifact: CapabilityArtifact, inputs, target, runs, interventions, operator_base_url,
                 evidence_dir, policy_path, headless):
    try:
        outcome = services.replay_capability(
            artifact=artifact,
            inputs=inputs,
            interventions=interventions,
            operator_base_url=operator_base_url,
            target_url_override=target,
            policy_path=policy_path,
            evidence_dir=evidence_dir,
            headless=headless,
            run_id=run_id,
        )
        record = runs.get(run_id)
        if record is not None:
            record.evidence_dir = str(outcome.evidence_dir)
        runs.complete(run_id, result=outcome.result.model_dump(mode="json"))
    except Exception as e:  # noqa: BLE001
        runs.fail(run_id, str(e))
