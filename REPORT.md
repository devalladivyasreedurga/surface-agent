# Design Write-Up

## 1. Architecture

The system is one Python process (three logical apps sharing it: the mock
bank target on :8000, an operator console on :8001, and a dashboard on
:8002), with a services layer (`computer_use/services.py`) as the single
place discovery and replay actually get wired up and run:

```
LLM (discovery only) --> DiscoveryAgent --> Surface (ABC) --> PlaywrightSurface --> live browser
                              |                  ^
                          ArtifactRecorder        |
                              |                  |
                       candidate artifact         |
                              |                  |
                    CapabilityRegistry.reconcile  |     (dedup/version against artifacts/)
                              |                  |
                          ReplayEngine -----------+   (no LLM import, ever)
                              |
                    PolicyEngine (gates every Surface.act, both paths)
                              |
                    InterventionManager <--> operator console (FastAPI, background thread)

services.discover_and_record() / replay_capability()
        ^                              ^
        |                              |
  cli/discover.py                cli/replay.py            <- one-shot processes
        ^                              ^
        +----------- dashboard/app.py (long-running) ------+
```

`cli/discover.py`, `cli/replay.py`, and the dashboard's HTTP handlers all
call the same two functions in `services.py` -- neither the CLI nor the
dashboard re-implements agent wiring. The difference is lifecycle: a CLI
invocation is a one-shot process that starts its own disposable operator
console; the dashboard is long-running and starts exactly one operator
console at startup, passing that same `InterventionManager` into every run
it kicks off in a background thread, so a human resuming an intervention
always lands on the console actually watching that run.

The key boundary is `Surface` (`surface/base.py`): an ABC with `observe()` and
`act()`. Neither `DiscoveryAgent` nor `ReplayEngine` imports Playwright --
only `PlaywrightSurface` does. This is what lets the write-up in Section 4
claim a `DesktopSurface` or `LegacyWebSurface` could be added later without
touching the agent loop, the artifact schema, or the replay engine: they
would just be new classes satisfying the same two methods.

The second boundary, arguably the more important one for this assignment, is
that `replay/` never imports `llm/`. "Replay makes zero LLM calls" is not a
runtime flag someone could leave on by accident -- there is no code path in
the replay package that can reach a model client. Determinism is a property
of the import graph, not a convention.

**Trade-off:** everything runs in one process with in-memory state
(`InterventionManager`, the operator console) rather than services/queues.
Section 9 of the brief explicitly discourages building scaling
infrastructure prematurely; a single process with clean interfaces is easier
to review and is what this assignment asks for. The seams (`Surface`,
`LLMClient`, `PolicyEngine`) are exactly where a real system would introduce
process/service boundaries later.

## 2. Artifact schema

`CapabilityArtifact` (`models/artifact.py`) is the reusable contract. Beyond
the required fields (steps, inputs, outputs, checkpoints), three choices are
deliberate:

- **Actions are generic, not Playwright code.** A step's `action` is one of
  `navigate/click/fill/extract/wait` with a locator description, never a
  selector string or recorded script. This is what keeps an artifact
  reviewable by a human who has never seen Playwright, and portable across
  surface implementations.
- **Locators are ranked, not single-strategy.** Each `Target` carries a
  `primary` locator plus `fallbacks`, each tagged with a `LocatorStrategy`
  (`accessibility` > `semantic` > `css_test_id` > `coordinate`) and a
  `confidence`. `Locator.model_post_init` forcibly downgrades any
  `coordinate` locator's confidence to `low` -- it is structurally impossible
  for a coordinate fallback to be mistaken for a robust locator later.
- **Parameterization via `{{input.x}}` placeholders**, not literal recorded
  values. `ArtifactRecorder` finds numeric tokens (3+ digits) in the goal
  text and replaces their literal occurrences in action values *and* in the
  inferred success checkpoint with placeholders, naming each parameter after
  the accessible label of the field it was typed into. This is a deliberate
  simplification, not a general slot-filling system -- documented in Section
  7 (Cuts). It works because every example goal in the assignment brief is
  shaped as "do X for identifier Y," and that is the case this heuristic
  targets.
- **Risk lives on the step**, not just the artifact, because one capability
  can mix safe reads and a step just short of an irreversible action (see
  Section 6).
- **No discovery-run reasoning or example values persisted.** Every step's
  `action.reasoning` is explicitly nulled by the recorder -- it's real,
  useful text (Claude's actual per-step rationale), but it belongs to *this
  run's* evidence log, not to a contract meant to be reused indefinitely.
  Same for the top-level `name`/`description`: the model is asked, as part
  of its `finish_success` call, to name the *reusable capability* in generic
  terms ("Look up member savings balance") rather than describe this run
  ("Recorded from a run looking up member 10001"). `sanitize_slug()` still
  regex-cleans whatever the model returns before it's used as a filename
  component, since free-form model output is not something to trust
  unvalidated for that.

`Step.checkpoint` (per step, optional) and `CapabilityArtifact.success_checkpoint`
(required, top-level) both use the same `Checkpoint` shape
(`expect_url_contains` / `expect_element` / `expect_text_contains`) so replay
has one verification code path, not two.

### Canonical identity, deduplication, and versioning

An artifact's identity is `capability_id` + `capability_version`, stored as
`artifacts/<capability_id>.v<version>.json`. `CapabilityRegistry` (not
`ArtifactRecorder`, deliberately -- see Section 1) owns every write and is
the only thing that decides whether a discovery run's output becomes a new
file:

1. Build a candidate from the trace (as above).
2. No existing version of that `capability_id` -> write `v1`.
3. An existing latest version whose **structural fingerprint** matches the
   candidate's -> don't write anything; append this run's id to that
   version's `additional_run_ids` (provenance) and re-save the *existing*
   file with that one field updated.
4. Otherwise -> write `v<latest+1>`; older versions are untouched.

The fingerprint (`capability_registry._fingerprint`) deliberately excludes
`name`, `description`, step descriptions, timestamps, `created_from_run_id`,
`additional_run_ids`, and each input's `example` value -- two runs of "look
up member X" for different X must fingerprint identically, or dedup would
never trigger for the one case it exists for. It includes `target.app_key`
(not `base_url` -- see Section 4), each step's action type/target/value/
risk/extract_as/checkpoint, and the top-level success checkpoint.

Verified live, not just unit-tested: running discovery for "member 10001"
created `lookup_member_savings_balance.v1`; running it again for "member
10002" (dashboard) and then again for "member 10001" via the CLI both
correctly reused v1 and only added their run ids to `additional_run_ids` --
one file on disk after three discovery runs. See `evidence/README.md`.

### The meaningful-replay-contract gate

A real bug surfaced while testing this by hand: running discovery for
"member 10003" (which doesn't exist) correctly finished with
`stop_reason: success` -- the agent correctly recognized "not found" as a
completed goal, per the system prompt -- but its 2-step trace (fill, click;
no extraction ever happened, since there was nothing to read) got
versioned as `v2` of the same capability, because it was structurally
different from `v1`. `v2`'s only checkpoint was a bare URL match
(`/members/search?member_id=...`), which is true regardless of outcome on
that route -- found, not found, or a validation error. Replaying `v2`
against *any* input reported a hollow `success` with empty outputs, because
it had no way to tell those states apart; it was never around to see one.

The fix, `artifact_recorder.has_meaningful_replay_contract()`, gates
*before* a candidate ever reaches `CapabilityRegistry.reconcile`: a
candidate is only persisted if it has at least one declared output backed by
a real extraction step, **or** a success checkpoint stronger than a bare URL
match (`expect_element` or `expect_text_contains`). A URL alone is never
sufficient -- the same route can represent multiple outcomes. When the gate
rejects a candidate, `services.discover_and_record` does not call the
registry at all: no file is written, no version is bumped, and the run's
full evidence (including which business outcome was detected, via the same
`error_taxonomy.detect_business_outcome` replay already uses) is still
saved, with a message like:

> Discovery completed with business outcome MEMBER_NOT_FOUND; no reusable
> capability was recorded because the successful execution path was not
> observed.

Covered by `tests/test_capability_gating.py` (a real mock_bank + Playwright
integration test, with a scripted fake `LLMClient` standing in for the
model so the test is deterministic and free): discovery against a
nonexistent member produces no artifact but keeps its evidence; an existing
`v1` is verified byte-for-byte unchanged by a gated run; replay of `v1`
against the same nonexistent member still correctly returns
`business_outcome: MEMBER_NOT_FOUND`; and, as a control, discovery against a
real member is still recorded normally -- the gate rejects the degenerate
case without being overly strict about the valid one.

**Known limitation, not fixed here:** this makes the *first* discovery run
against a since-nonexistent identifier a dead end -- it produces evidence
but no capability, and someone has to re-run discovery against a valid
identifier to get a usable artifact. A better version would let a
not-found-only run stand as a *provisional* capability that a later
successful run then upgrades in place once it observes the success branch,
rather than requiring the successful run to happen first. See Section 7.

### Checkpoints express semantic state, not just URL

Checked deliberately, not assumed: does anything in the checkpoint
abstraction fundamentally require navigation or a URL change to represent a
state transition? No -- and this was verified by tracing the actual code,
not asserted from design intent. `Checkpoint.expect_url_contains` is one of
six independent optional fields, evaluated by `PlaywrightSurface.
check_condition` behind an `if url_contains and ...` guard identical in
shape to every other field's guard -- nothing short-circuits on it or
treats it as primary. The fields, and what each one actually tests:

- `expect_url_contains` -- URL substring match. Optional, supplementary,
  never required (see the gate above: a candidate whose *only* signal is
  this one is rejected before it can be persisted).
- `expect_element` -- resolves in the accessibility tree. Presence, not
  visibility -- note below on what that means in practice.
- `expect_element_visible` -- resolves **and** is actually rendered
  (non-empty box, not `visibility:hidden`). Stricter than `expect_element`;
  distinguishes "the value exists in the tree" from "the value is actually
  on screen" -- e.g. a loading placeholder collapsed to zero size before its
  real content arrives.
- `expect_text_contains` -- substring of `Page.inner_text("body")`, which
  Playwright already restricts to *rendered* text, so this is effectively a
  "text visible" check, not "text present in markup."
- `expect_output_present` -- the one condition type with no Surface call at
  all: asserts a named output this replay run already captured is non-empty.
  Pure engine-side bookkeeping, evaluated identically regardless of whether
  the surface has any notion of a URL, a DOM, or a screen.
- `any_of: list[Checkpoint]` -- alternatives, recursively evaluated: the
  checkpoint passes if its own direct fields all pass (the existing multi-
  field-set-on-one-Checkpoint behavior already **is** `all_of`, so no
  separate field for that was needed) **or** if any one of these does.

**Why this answers the SPA / legacy-shared-route / desktop question
directly:** an SPA whose URL never changes can still express a real
checkpoint via `expect_element_visible` or `expect_output_present` alone,
with `expect_url_contains` simply omitted (the recorder only sets it when
`SurfaceState.url` is non-empty -- `agent/artifact_recorder.py,
_infer_success_checkpoint`, changed to stop force-populating it). A legacy
app that reuses one route for multiple outcomes is exactly the case
`has_meaningful_replay_contract` exists to catch -- a URL-only checkpoint on
such a route is now rejected at record time, not silently wrong at replay
time. A hypothetical `DesktopSurface` with no URL concept at all would
report `SurfaceState.url == ""`; every URL check against that is skipped
(empty string is falsy), and `expect_output_present` works completely
unchanged, since it never asks the surface anything.

**One honest caveat found while testing this, not smoothed over:** for the
`ACCESSIBILITY` locator strategy specifically, `expect_element` and
`expect_element_visible` often coincide in practice. Chromium excludes
`display:none` and `visibility:hidden` elements from the accessibility tree
*entirely* -- a role+name query returns zero matches for either, so
`expect_element` already fails on them too, and the stricter check adds
nothing. The real gap (proven in
`tests/test_playwright_surface_visibility.py`) shows up for elements the
tree *does* expose but that have no rendered box yet (e.g. a zero-size
loading placeholder) -- that is the genuine "present but not yet visible"
case, not CSS-hidden content, which never reaches `expect_element` in the
first place given this project's accessibility-first strategy. Worth
knowing before assuming the two fields diverge as often as their names
might suggest.

## 3. Determinism & error handling

Determinism comes from three things holding simultaneously: replay never
calls an LLM (structural, see Section 1); locator resolution requires an
**exact, unique** match before falling through the ranked strategy chain
(`surface/locator_resolver.py`); and every input parameter is resolved
through one shared `{{input.x}}` substitution function
(`computer_use/params.py`) used identically for action values and checkpoint
text, so a checkpoint written against the recorded member always generalizes
correctly to a different one at replay time.

**The locator-ambiguity bug I found and fixed while testing:** the first
version of `_resolve_one` fell back to `.first` whenever a role+name query
matched more than one element. Against the mock bank's layout table, an
outer layout `<td>`'s computed accessible name is the concatenation of all
its descendant text, so a non-exact substring match for "Savings Balance
Value" matched *both* that outer container and the actual value cell --
and `.first` silently picked the wrong one (the outer container, in DOM
order). The fix: every strategy attempt now requires `count() == 1` (trying
`exact=True` before `exact=False` for accessibility) and treats ambiguity as
"this strategy did not resolve," falling through to the next fallback and
ultimately to `UnresolvedTargetError` rather than ever guessing among
matches. Acting on the wrong element with false confidence is worse than
failing loudly.

**Checkpoints do real verification work, not just a final URL check.**
`ArtifactRecorder` attaches an `expect_element` checkpoint (the final
extraction step's own locator) to two places: the step immediately before
the extraction, and the top-level `success_checkpoint` (alongside its URL
pattern). This is deliberately the *same* locator reused, not an invented
one -- it is real, observed-in-this-run structure, and it means replay
fails fast at the "did we actually reach a usable page" step rather than
discovering the problem only when extraction itself fails one step later.
Verified live: replaying the not-found/validation-error/permission-denied/
hard-failure scenarios now all resolve at that earlier checkpoint step
(1 of 3 steps completed) instead of at the extraction step.

**A second real bug, found while generating evidence for the checkpoint
change:** giving checkpoints their own verification path (`Surface.
check_condition`, separate from `Surface.act`) meant checkpoint verification
had no way to recover from a known interstitial the way `act()` already
could -- replaying the "member 70000, notice-on-first-view" scenario started
coming back `hard_failure` instead of `success`, because the new step_2
checkpoint hit the interstitial and had no dismiss-and-retry logic of its
own. Fixed by giving `check_condition` the same one-shot
dismiss-known-interstitial-and-recheck behavior `act()` has, and by changing
its return type to `(ok, detail, recovered)` so a recovered checkpoint now
produces a proper `RecoveryEvent` in the result instead of passing silently
-- checked, since silent recovery would have been strictly worse than what
it replaced. Both the regression and the fix are covered by
`tests/test_playwright_surface_recovery.py`, and the confirmed-working
scenario is in `evidence/README.md`.

**Error taxonomy (`models/results.py`, `ReplayEngine`, `replay/error_taxonomy.py`):**

- `success` -- checkpoints verified, outputs returned.
- `business_outcome` -- a step or the success checkpoint failed to match,
  *and* the current page text matches a known pattern (`MEMBER_NOT_FOUND`,
  `VALIDATION_ERROR`, `PERMISSION_DENIED`). Detected via a small deterministic
  regex table, not an LLM call. Documented as app-specific: the mechanism
  (pattern-match over observed text, no model) generalizes across apps;
  the pattern table itself is per `app_key`, matching the multi-tenant story
  in Section 4.
- `hard_failure` -- nothing recoverable matched; returns `step_id`,
  `expected`, `observed`, an `error_code`, and a screenshot reference.
- **Recoverable conditions are not a fourth status.** They are logged as
  `RecoveryEvent`s and the run continues. Two are implemented for real, not
  just described:
  - *Transient slow load*: `PlaywrightSurface._recover_from_timeout` catches
    a timeout, then actively polls a live `location.href` read (not the
    cached `page.url`, which I found empirically can keep reporting the
    pre-navigation address for seconds after the browser has actually moved
    on) to see whether the action's effect already landed server-side before
    deciding whether to retry the action or accept it as recovered. This
    matters because blindly re-clicking a control that already triggered a
    server-side effect risks a double action -- exactly the failure mode a
    real back-office app would punish.
  - *Known interstitial*: any element exposing ARIA role `alertdialog` is
    dismissed automatically (clicking its first button) before the original
    action is retried once.
- Verified against 8 concrete scenarios in the mock bank, all against the
  one deduplicated `lookup_member_savings_balance.v1` artifact (valid x2 with
  different inputs, not-found, validation error, permission denied, slow
  load, interstitial, simulated server error) -- see `/evidence/`.

**UI drift** is out of scope by design of the target (Section 1 of the
brief: stable UIs, real runtime errors). The ranked locator strategy is the
mitigation that exists for it anyway: if a css/test-id selector breaks, the
accessibility/semantic locators typically still resolve.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** `Surface.observe/act` and the generic `Action`
vocabulary are the seam. A `LegacyWebSurface` (framesets, table layouts) can
still speak the same `Action` types over Playwright's frame API; a
`DesktopSurface` would implement `observe()` over OS accessibility APIs
(role/name concepts exist there too) and `act()` over OS-level input
injection. Neither the artifact schema nor `ReplayEngine` would change --
only which concrete `Locator` values a recorder for that surface produces.
`ElementInfo` deliberately mirrors accessibility-tree vocabulary (`role`,
`name`) rather than DOM vocabulary (`tag`, `class`) for exactly this reason.

**Multi-tenant reuse.** `ApplicationTarget.app_key` is the vendor/product
identity, separate from `base_url` (the tenant's instance). The intended
reuse path: replay the same artifact against a different tenant's `base_url`
for the same `app_key` via `--target` (implemented: `replay.py --target`
overrides `artifact.target.base_url`, and the CLI added that host to the
policy allowlist for the run). What is *not* built (Section 8 stretch,
deliberately cut): per-variant override records for when a tenant's
branding/copy genuinely differs (e.g. a different not-found message string
that the `error_taxonomy` pattern table would need to know about, or a
relabeled field changing a locator's accessible name). The natural extension
is an artifact keyed by `app_key` plus a small per-tenant override map
(pattern-table entries, locator substitutions) rather than a separate
artifact per tenant -- drift detection would replay a canary set of
capabilities per tenant on a schedule and flag `hard_failure` upgrades as a
signal that a tenant's app version has moved.

## 5. Escalation & handoff

One `InterventionManager` per process, shared by the agent/replay loop and a
FastAPI operator console running in a background thread of the *same*
process. Requesting an intervention blocks the calling thread on a
`threading.Event`; the operator console's resume endpoint is what sets it.
There is never a second browser session -- `PlaywrightSurface` holds exactly
one `Page` for the run's lifetime, and the human interacts with that same
visible, headful window directly. The console is only the signaling/context
layer (goal, current step, reason, before-screenshot) around that, not a
replacement for it.

Verified for real (not just described): a replay run was backgrounded, it
paused on a policy-flagged step, the operator console (`curl` standing in for
a human) showed the pending intervention with full context and a
before-screenshot, a `POST /resume` unblocked the waiting process from a
separate shell, and the run completed -- with `escalation_raised` and
`escalation_resumed` events plus before/after screenshot references in the
structured evidence log.

Two trigger paths exist: `DiscoveryAgent` escalates when the LLM calls
`finish_stuck`; both `DiscoveryAgent` and `ReplayEngine` escalate when
`PolicyEngine` returns `requires_confirmation` for a risky/irreversible
action, *before* executing it.

**Cut (documented, not hidden):** we do not record the human's individual
mouse/keyboard actions while they hold control -- only an optional free-text
note plus before/after screenshots. The scope note in the brief explicitly
allows this. A full action-level recording would mean instrumenting the
Playwright page's input event stream during the paused window, listed below
as a next step.

## 6. Safety

`PolicyEngine.evaluate` runs before every `Surface.act`, in both discovery
and replay, and is the final authority -- not the model. Two independent
checks, deliberately not derived from each other:

- **Allowlist**: action type must be permitted, and the target domain (for
  `navigate`, the destination; otherwise the current page's domain) must be
  in `allowed_domains` (optionally further restricted by `allowed_routes`
  regexes). Configured in `config/allowlist.json`.
- **Risk classification**: a keyword scan (`transfer`, `wire`, `withdraw`,
  `close account`, `submit payment`, ...) over the action's value/reasoning/
  locator text, **combined with, but not overridden by**, the artifact's own
  declared `Step.risk`. An artifact step mis-tagged (or adversarially
  tagged) `safe` cannot bypass a keyword hit; the keyword scan runs
  regardless of what the artifact claims. If either signal says
  `irreversible`, the configured policy (`block` or, by default,
  `require_confirmation`) applies -- confirmation routes through the same
  escalation mechanism as Section 5, so a human approves *before* execution,
  not after.

The mock bank includes a real (never-recorded) irreversible endpoint --
`POST /members/{id}/sub-account/confirm` -- specifically so this mechanism
has something concrete to point at: no capability in this project ever
targets it, and the recorded lookup capability's steps are all `safe`.

**Redaction** (`safety/redaction.py`) runs on every log line and saved JSON
blob: key-based redaction for known-sensitive field names, pattern-based for
SSN/card-number/bearer-token/API-key shapes in free text. **Explicit limit,
stated rather than glossed over:** screenshots are not pixel-redacted --
exposure is reduced by only capturing them on observe/failure/escalation
rather than every step, not eliminated. Real image-level redaction is listed
below as a next step.

## 7. Cuts

- **Parameterization is a numeric-token heuristic**, not general slot
  filling. Works for every example goal shape in the brief; would need a
  more general approach (e.g. explicit slot annotations from the model) for
  goals with non-numeric caller-supplied values.
- **Canonical capability naming depends on the model complying with
  instructions**, mitigated but not eliminated by `sanitize_slug()`'s
  regex cleanup and a three-level fallback chain (model's slug -> model's
  name, slugified -> the goal text, slugified) if the model ever omits the
  new `finish_success` fields. Observed reliably correct in every live run
  in `/evidence/`, but it's a prompted behavior, not a schema-enforced one --
  a stricter version would validate the returned slug against a regex and
  retry the tool call if it fails, which isn't built.
- **No pixel-level screenshot redaction.** Exposure reduced by capture
  policy, not eliminated.
- **No per-action human recording during escalation** -- before/after
  screenshots and an optional note, not a full input-event trace. Allowed
  explicitly by the brief's scope note.
- **Business-outcome patterns are per-app, hand-written**, not learned or
  generalized automatically across tenants. The mechanism generalizes; the
  pattern table does not yet.
- **No multi-tenant override records or drift detection are implemented**,
  only designed (Section 4) -- correctly out of scope per the brief.
- **Only one surface (`PlaywrightSurface`) is implemented.** `DesktopSurface`/
  `LegacyWebSurface` are designed-for, not built, per Section 3.7.
- **No automatic goal-to-capability routing.** Discovery always calls the
  LLM; replay always requires picking a specific saved capability (CLI: a
  file path; dashboard: a list). A goal is never automatically matched
  against an existing capability to decide discovery-vs-replay for you --
  deliberately deferred; see `services.py`'s docstring for where a
  `resolve(target, goal) -> capability_id | None` router would slot in
  without touching either code path it sits in front of.
- **The dashboard is deliberately thin**: server-rendered HTML, polling
  (`<meta refresh>`) for live status rather than websockets/SSE, no auth, no
  pagination. It reads a running discovery/replay's progress by tailing that
  run's own evidence `log.jsonl` rather than a separate progress-event
  channel -- reuses evidence that already exists instead of adding a new
  reporting path, at the cost of only being as fine-grained as what's
  already logged.
- **A capability can only be recorded from a discovery run that observed the
  full success path**, enforced by `has_meaningful_replay_contract` (Section
  2). A run that only ever saw a business outcome (e.g. "member not found")
  produces no capability at all, even though the run itself succeeded at its
  goal -- see the limitation note in Section 2 on why a *provisional*
  capability, upgraded in place by a later successful run, is the natural
  next step rather than the current hard reject.
- **What I'd build next**: per-tenant artifact override records with drift
  detection via scheduled canary replays; a confidence/approval gate
  (draft -> approved) before unattended replay, scored from multi-run replay
  stability; genuine action-level recording during human escalation; the
  goal-to-capability router noted above; and merging observations across
  multiple discovery runs of the *same* capability into one richer artifact
  -- today each run either matches the latest version's structure exactly
  (dedup) or doesn't (new version), with no middle path. A run that
  discovers the success branch and a separate run that discovers a business
  outcome branch are two different structural fingerprints today, even
  though a real capability legitimately has both -- replay already handles
  that fine at *runtime* (the same artifact's checkpoint failure correctly
  falls through to business-outcome detection), so the gap is specifically
  in what discovery is able to *record* from a single run, not in what
  replay can *handle*. A merge step would need to recognize that two
  candidates differ only in "where the observed trace stopped" (one
  continued to extraction, one stopped at a checkpoint) rather than in the
  shared steps both actually walked through, and combine them into one
  artifact instead of forcing a strict match-or-version-bump choice.
