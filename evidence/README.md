# Evidence index

Every directory here is the real, unedited output of running discovery/replay
through either the CLI or the dashboard (`computer_use.cli.dashboard`) against
the mock bank. Nothing was hand-written after the fact.

## Artifact lifecycle (discovery -> canonical capability, deduplicated)

- **`discovery_d7bc2d199c/`** -- goal: *"Look up member 10001 and read the
  savings balance"*, run via the dashboard. Claude's `finish_success` call
  proposed the capability itself: slug `lookup_member_savings_balance`, name
  "Look up member savings balance", a generic description -- none of it
  mentions member 10001. This created
  `artifacts/lookup_member_savings_balance.v1.json`.
- **`discovery_2c79ebe834/`** -- same goal, different member (*"...member
  10002..."*), also via the dashboard. Structurally identical flow ->
  **no new artifact was written**; this run's id was appended to v1's
  `additional_run_ids` instead. Dashboard showed "reused existing
  lookup_member_savings_balance.v1".
- **`discovery_53b1bb697c/`** -- same goal as the first run, this time via
  the CLI (`computer_use.cli.discover`), proving discovery-run deduplication
  isn't a dashboard-only behavior -- both entry points call the same
  `computer_use.services.discover_and_record`, and this run also deduplicated
  into v1 (now with 3 provenance run ids: 1 creator + 2 additional).

Check `artifacts/lookup_member_savings_balance.v1.json` directly: one file,
`created_from_run_id` = the first run, `additional_run_ids` = the other two.
No literal member id, extracted value, or LLM reasoning appears anywhere in
it -- compare against `discovery_d7bc2d199c/log.jsonl`, which has the full,
unredacted reasoning for every step (that's where it's supposed to live).

## Replay (deterministic, zero LLM calls) -- full error taxonomy

All replay against `artifacts/lookup_member_savings_balance.v1.json`:

| Directory | Input | Result |
|---|---|---|
| `replay_44c4c6b6e1` | `member_id=10001` (via dashboard) | `success` |
| `replay_b479987291` | `member_id=10002` (via CLI, different member than recorded) | `success` |
| `replay_8c375e648e` | `member_id=99999` (via dashboard) | `business_outcome: MEMBER_NOT_FOUND` |
| `replay_9f562ac61c` | `member_id=abc12` | `business_outcome: VALIDATION_ERROR` |
| `replay_09c161ff1a` | `member_id=50000` | `business_outcome: PERMISSION_DENIED` |
| `replay_1b93fc186a` | `member_id=99999999` | `hard_failure` -- simulated server error, caught by the step_2 checkpoint, with screenshot evidence |
| `replay_e0715f37a1` | `member_id=60000` | `success`, with a `transient_slow_load` `RecoveryEvent` |
| `replay_6b15d69d75` | `member_id=70000` | `success`, with a `known_interstitial_dismissed` `RecoveryEvent` -- caught by the step_2 checkpoint's own recovery path (see below) |

Note the business-outcome and hard-failure cases resolve at **step_2** (1 of
3 steps completed), not step_3: the checkpoint attached to the step right
before extraction (`expect_element` = the same locator the final extract
targets) catches these before an extraction is even attempted, per the
"verify member details OR detect known business outcome" requirement.

**A real bug found and fixed while generating this evidence:** the first
pass at `replay_6b15d69d75` (member 70000, interstitial) came back as
`hard_failure`, not `success`. Adding a checkpoint on step_2 meant checkpoint
verification now had its own path to the page that could hit the
interstitial -- but only `PlaywrightSurface.act()` knew how to dismiss a
known interstitial and retry; `check_condition` (checkpoint verification)
didn't. Fixed by giving `check_condition` the same recovery attempt
`act()` already had, and surfacing it as a proper `RecoveryEvent` rather than
a silent pass. See `REPORT.md`, Determinism & error handling.

## Escalation / human handoff (genuine pause + resume)

- **`replay_91f3627425`** replays
  `artifacts/lookup_member_savings_balance__escalation_demo.v1.json` -- a
  copy of the real v1 capability with step_3's risk manually elevated to
  `irreversible`, solely so there is something to escalate on (the real
  capability is read-only and correctly has no risky steps). The run paused
  mid-replay; `log.jsonl` shows `escalation_raised` with the intervention id
  and operator URL, a real human action (`curl -X POST .../resume`, standing
  in for a click on the operator console) unblocked the still-running
  process from a separate shell, and `escalation_resumed` plus the completed
  result are in the same log -- the pause/resume crosses a real process
  boundary, not an in-memory callback.
