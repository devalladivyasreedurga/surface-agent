# Computer-Use Automation System

A small, real, end-to-end vertical slice: an LLM-driven discovery agent that
operates a live web app, canonicalizes what it did into a typed, versioned,
**deduplicated** capability artifact, and a deterministic replay engine that
re-runs that artifact with zero LLM calls -- handling business outcomes,
transient errors, and hard failures explicitly, with a real (if minimal)
human-escalation handoff. A minimal dashboard sits on top for
discovery/replay/capability-browsing; the CLI (kept for scripting/evaluation)
and the dashboard call the exact same service functions underneath.

See [`REPORT.md`](REPORT.md) for the design write-up (architecture, schema,
determinism/error handling, multi-tenant story, escalation, safety, cuts).

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY (only needed for the discovery step;
# replay makes zero LLM calls and needs no key at all)
```

## Running without live services

The **replay** path needs no LLM/API key -- only the mock bank app running
locally. **Discovery** additionally needs `ANTHROPIC_API_KEY` set in `.env`,
since it makes real Claude API calls to decide each step.

## Three separate applications

| App | Port | Role |
|---|---|---|
| Mock bank (`mock_bank/app.py`) | 8000 | the target being automated |
| Operator console | 8001 | human intervention/handoff (real, separate live Playwright session) |
| Dashboard (`computer_use.cli.dashboard`) | 8002 | discovery, capability catalog, replay -- the control/observability surface |

They run as separate processes/ports by convention but share one codebase;
the dashboard also starts its own operator console instance at startup so
every run it kicks off shares one `InterventionManager` (a CLI-initiated run
starts a fresh, disposable operator console per invocation instead).

**Important:** the dashboard is a status/control surface only -- it never
embeds the mock bank or the automation browser in an iframe. The actual
browser Playwright drives is a separate, real, visible window (headful by
default) that you watch directly; the dashboard just shows text status
derived from that run's live evidence log.

## Demo path

Terminal 1 -- start the target application:

```bash
source .venv/bin/activate
uvicorn mock_bank.app:app --host 127.0.0.1 --port 8000
```

Terminal 2 -- start the dashboard (also starts its own operator console on 8001):

```bash
source .venv/bin/activate
python -m computer_use.cli.dashboard
```

Open **http://127.0.0.1:8002**:
- **Discover**: enter a target URL + free-text goal, click Run Discovery. A
  real headful browser opens; the page polls the run's live status (current
  step/action) until it finishes, then shows which capability was
  created/reused.
- **Capabilities / Replay**: lists saved capabilities in human-readable form
  (name, description, typed inputs/outputs). Click one to get a form with
  exactly its declared inputs, submit to replay it (zero LLM calls).
- **Interventions**: links out to the operator console for any pending human
  handoff, plus a simple pending/resolved list.

### Or drive it from the CLI (same underlying services, useful for scripting/evaluation)

```bash
python -m computer_use.cli.discover \
  --target http://127.0.0.1:8000 \
  --goal "Look up member 10001 and read the savings balance"

python -m computer_use.cli.replay \
  --artifact artifacts/lookup_member_savings_balance.v1.json \
  --target http://127.0.0.1:8000 \
  --input member_id=10002
```

Both CLI commands and the dashboard call the exact same
`computer_use.services.discover_and_record` / `replay_capability` functions
-- see `src/computer_use/services.py`.

## Artifact lifecycle: canonical, deduplicated, versioned

A discovery run does **not** always produce a new file. On success:

1. The model itself proposes a generic capability identity as part of its
   `finish_success` call -- slug, name, and description, all abstracted away
   from the specific goal's literal values (e.g. goal *"Look up member 1008
   and read the savings balance"* -> slug `lookup_member_savings_balance`,
   not `look_up_member_1008_and_read_the_savings_balance`).
2. `ArtifactRecorder` builds a candidate artifact from the run's trace, with
   every literal caller-supplied value replaced by `{{input.x}}` and all
   discovery-run reasoning stripped (it stays in the evidence log, not the
   artifact).
3. `CapabilityRegistry.reconcile` compares that candidate's **structure**
   (steps, locators, inputs, outputs, checkpoints -- not wording, not example
   values, not timestamps) against the latest existing version of that
   capability id:
   - No existing version -> save as `<id>.v1.json`.
   - Structurally identical to the latest version -> **no new file is
     written**; this run's id is appended to that version's
     `additional_run_ids` for provenance, and evidence is still kept in full
     for this run.
   - Structurally different -> saved as `<id>.v<N+1>.json`; older versions
     are left untouched.

Discovery evidence (`evidence/discovery_<run_id>/`) is written for **every**
run regardless of dedup outcome. Capability artifacts (`artifacts/`) are not.

**Only a run that observed a real "found" state gets recorded as a
capability at all.** If the goal's identifier happens not to exist, the
agent correctly finishes successfully (recognizing "not found" as a
legitimate answer) -- but that run never saw what a found result looks
like, so persisting it as a reusable capability would produce something
that can only ever report a hollow success on replay. Discovery still keeps
the evidence and tells you why nothing was saved, e.g. *"Discovery
completed with business outcome MEMBER_NOT_FOUND; no reusable capability
was recorded because the successful execution path was not observed."*
See `REPORT.md`, Artifact schema.

### Exercising the error taxonomy

The mock bank has deterministic scenarios keyed by member ID, so you can
replay the **same artifact** against each and see every outcome class without
re-recording anything:

| `--input member_id=` | Result |
|---|---|
| `10001` / `10002` | `success`, with the extracted balance |
| `99999` | `business_outcome` -- `MEMBER_NOT_FOUND` |
| `abc12` | `business_outcome` -- `VALIDATION_ERROR` (non-numeric id) |
| `50000` | `business_outcome` -- `PERMISSION_DENIED` (restricted/compliance hold) |
| `60000` | `success`, with a `transient_slow_load` recovery event (first view is slow) |
| `70000` | `success`, with a `known_interstitial_dismissed` recovery event (first view shows a policy notice) |
| `99999999` | `hard_failure` -- simulated server error, caught by the pre-extract checkpoint (with screenshot evidence) |

`60000` and `70000` are "first view only" -- reset them with:

```bash
curl -X POST http://127.0.0.1:8000/admin/reset
```

### Human escalation

If a step's declared risk is `irreversible`, or an action matches a risky
keyword (see `config/allowlist.json`), the policy engine pauses the run
*before* executing it and prints an operator console URL, e.g.:

```
[ESCALATION] risky action requires confirmation: ...
[ESCALATION]   http://127.0.0.1:8001/interventions/<id>
```

Open that URL to see the goal, the current step, the reason, and a
screenshot of the live session at the moment it paused. The automation is
not on a separate session -- it's the same visible browser window sitting on
your screen; you can interact with it directly. Clicking **Resume** on the
operator page unblocks the waiting process and it continues from there.

The recorded lookup capability is read-only and correctly has no risky
steps, so there is nothing in it to naturally escalate on. To demonstrate the
mechanism for real anyway,
`artifacts/lookup_member_savings_balance__escalation_demo.v1.json` is a copy
of the genuine artifact with one step's risk manually elevated to
`irreversible`:

```bash
python -m computer_use.cli.replay \
  --artifact artifacts/lookup_member_savings_balance__escalation_demo.v1.json \
  --input member_id=10001 --headless
```

## Evidence

`/evidence/` contains the actual output of the runs above -- three real
discovery runs (one via the dashboard that creates the capability, one via
the dashboard and one via the CLI that both correctly deduplicate into it),
plus replay runs covering every outcome class, and one live escalation
pause/resume. See [`evidence/README.md`](evidence/README.md) for an indexed
walkthrough of which directory demonstrates what -- including a real bug
found and fixed while generating it.

## Project layout

```
mock_bank/            the target application (FastAPI + Jinja, intentionally legacy-feeling)
src/computer_use/
  models/              Action, SurfaceState, CapabilityArtifact, results -- the typed contracts
  surface/             Surface ABC + the one implementation, PlaywrightSurface
  llm/                 LLMClient ABC + AnthropicLLMClient (discovery only)
  agent/               DiscoveryAgent loop, ArtifactRecorder, CapabilityRegistry (dedup/versioning)
  replay/              ReplayEngine + business-outcome pattern matching (no LLM import)
  safety/              PolicyEngine (allowlist + risk classification) + redaction
  escalation/          InterventionManager + a minimal mocked operator console
  evidence/            structured, redacted run logging
  dashboard/           minimal FastAPI UI: Discover / Capabilities-Replay / Interventions
  services.py          shared discover_and_record() / replay_capability(), used by CLI + dashboard
  cli/                 discover.py / replay.py / dashboard.py entry points
config/allowlist.json  policy configuration
artifacts/             saved capability artifacts (<id>.v<N>.json)
evidence/              per-run structured logs + screenshots
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```
