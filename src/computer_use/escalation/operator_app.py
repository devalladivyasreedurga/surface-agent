"""Minimal, deliberately mocked operator console.

Scope note (per assignment Section 3.6): a full real-time co-browsing
console is out of scope. This gives a human enough to act on an intervention
-- what stopped, where, a screenshot, a place to leave a note -- and a
Resume button. The actual "take control" step is the human using the same
already-open, headful browser window on their own screen; this UI is only
the signaling/context layer around that, not a replacement for it.
"""
from __future__ import annotations

import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from computer_use.escalation.intervention import InterventionManager, InterventionStatus

_PAGE_STYLE = """
<style>
body { font-family: system-ui, sans-serif; margin: 2rem; color: #222; }
.card { border: 1px solid #ccc; border-radius: 6px; padding: 1rem; margin-bottom: 1rem; }
.pending { border-left: 4px solid #b33; }
.resumed { border-left: 4px solid #3a3; opacity: 0.75; }
img { max-width: 480px; border: 1px solid #999; display: block; margin: 0.5rem 0; }
textarea { width: 100%; max-width: 480px; }
</style>
"""


def build_operator_app(manager: InterventionManager) -> FastAPI:
    app = FastAPI(title="Operator Console (mocked)")

    @app.get("/", response_class=HTMLResponse)
    def index():
        items = manager.list_all()
        rows = []
        for r in items:
            css = "pending" if r.status == InterventionStatus.PENDING else "resumed"
            rows.append(
                f'<div class="card {css}"><b>{r.reason.value}</b> &mdash; run {r.run_id} '
                f'(<a href="/interventions/{r.id}">details</a>) &mdash; status: {r.status.value}</div>'
            )
        body = "".join(rows) or "<p>No interventions yet.</p>"
        return f"<html><head>{_PAGE_STYLE}</head><body><h1>Operator Console</h1>{body}</body></html>"

    @app.get("/interventions/{intervention_id}", response_class=HTMLResponse)
    def detail(intervention_id: str):
        r = manager.get(intervention_id)
        if r is None:
            return HTMLResponse("<p>Not found.</p>", status_code=404)
        img = (
            f'<img src="/interventions/{r.id}/screenshot/before">'
            if r.before_screenshot_ref
            else "<p>(no screenshot captured)</p>"
        )
        resume_form = (
            ""
            if r.status == InterventionStatus.RESUMED
            else f"""
            <form method="post" action="/interventions/{r.id}/resume">
              <label for="note">What did you do? (optional)</label><br>
              <textarea id="note" name="note" rows="3"></textarea><br>
              <button type="submit">Resume automation</button>
            </form>
            """
        )
        return f"""<html><head>{_PAGE_STYLE}</head><body>
        <p><a href="/">&laquo; back</a></p>
        <h1>Intervention {r.id}</h1>
        <p><b>Run:</b> {r.run_kind} / {r.run_id}</p>
        <p><b>Goal/capability:</b> {r.goal_or_capability}</p>
        <p><b>Current step:</b> {r.current_step or '(n/a)'}</p>
        <p><b>Reason:</b> {r.reason.value} &mdash; {r.reason_detail}</p>
        <p><b>State URL at pause:</b> {r.state_url}</p>
        <p><b>Status:</b> {r.status.value}</p>
        {img}
        {resume_form}
        {'<p><b>Operator note:</b> ' + r.operator_note + '</p>' if r.operator_note else ''}
        </body></html>"""

    @app.post("/interventions/{intervention_id}/resume")
    def resume(intervention_id: str, note: str = Form("")):
        manager.resume(intervention_id, operator_note=note or None)
        return RedirectResponse(url=f"/interventions/{intervention_id}", status_code=303)

    @app.get("/interventions/{intervention_id}/screenshot/{which}")
    def screenshot(intervention_id: str, which: str):
        r = manager.get(intervention_id)
        ref = None
        if r is not None:
            ref = r.before_screenshot_ref if which == "before" else r.after_screenshot_ref
        if not ref or not Path(ref).exists():
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(ref)

    return app


def start_operator_server_in_background(app: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server
