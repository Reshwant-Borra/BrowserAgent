# Phase 2 Report — Verification, Idempotency, Recovery

## Verdict: **PASS**

Every exit-gate item is implemented and covered by a passing test that exercises real
Playwright against the fixture site (not a mock of the capability under test).

## What was built

- `agent/verifier.py` — deterministic `expected_result` vs. actual `PageObservation` checks
  (`url_contains`, `page_contains`, `element_present`, `element_absent`, `title_contains`).
  Ordinary code, no LLM call — the model never judges its own success.
- `browser/state_hash.py` — a normalized fingerprint (url + title + ordered
  `(role, name, disabled, selected, checked)` tuples + a visible-text sample) deliberately
  excluding numeric element ids (positional, reassigned every observation) and raw HTML
  (volatile-attribute noise).
- `agent/recovery.py` — the `NORMAL → RETRY → REFRESH_STATE → DEEP_RECOVERY →
  REPLAN_REQUIRED → USER_REQUIRED` ladder, plus the idempotency policy (Cases A/B/C from
  ARCHITECTURE.md §14): safe-retry only when the page genuinely didn't change; escalate
  (never silently retry) when it did but verification still failed; never auto-retry a
  CONSEQUENTIAL action a second time, regardless of A/B.
- `agent/loop_detector.py` — no-op, repeated-action, navigation-loop (A↔B↔A↔B), and modal-
  obstruction detection, all over the persisted `recent_actions` window (works identically
  before and after a crash/resume, since it reads from persisted state, not live memory).
- `agent/schemas.py`'s `classify_risk()` — a conservative, generic
  READ_ONLY/LOW_RISK_WRITE/CONSEQUENTIAL classifier based on action type + a keyword scan
  of the target element's name (submit/buy/delete/send/publish/etc.), not per-site rules.
- CLI approval gate (`agent/loop.py::_prompt_for_approval`) for CONSEQUENTIAL actions,
  configurable via `browser.interactive_approval`.

## Test results

```
tests/unit    (agent/recovery.py, agent/loop_detector.py, agent/verifier.py — 21 tests)
tests/integration/test_phase2_verification_recovery.py   5 passed
```

Exit-gate checklist:

1. **Every action can carry an expected result** — PASS (`ExpectedResult`, all 5 assertion
   types, unit-tested in `tests/unit/test_verifier.py`).
2. **Verification is deterministic** — PASS. No LLM involvement; pure function over two
   plain data structures.
3. **Failed actions are distinguishable from successful ones** — PASS. `VerificationResult.passed`
   + per-check breakdown, persisted as its own `VERIFICATION_RESULT` event type.
4. **No-op detection works** — PASS: `test_noop_button_triggers_recovery_escalation` clicks a
   real button whose `onclick` does nothing, against a real page, and confirms recovery
   escalates away from `normal` and stays escalated even after the task completes (finish
   does not silently reset it).
5. **Repeated-action loop detection works** — PASS (unit-tested; `detect_repeated_action`,
   `tests/unit/test_loop_detector.py`).
6. **Navigation-loop detection works** — PASS: `test_navigation_loop_is_detected` drives four
   real clicks bouncing between two real fixture pages and confirms a `RECOVERY_TRANSITION`
   event with `reason: "loop_detected"` is recorded.
7. **Consequential actions are protected against blind retries** — PASS:
   `test_consequential_action_is_not_auto_retried_after_failure` clicks a real "Submit
   Application" button (classified CONSEQUENTIAL via the keyword "submit"), the click fails
   its (deliberately impossible) verification, a second identical click attempt is blocked
   *before* it ever reaches Playwright — confirmed via the event log: exactly one
   `ACTION_RESULT` event exists for that action's fingerprint, not two.
8. **Recovery state transitions are tested** — PASS (`tests/unit/test_recovery.py`: full
   ladder progression, loop-jump-to-REFRESH_STATE, USER_REQUIRED terminality, all three
   idempotency cases).
9. **All Phase 1 tests still pass** — PASS (verified via the combined `pytest -m "not model"`
   run in `docs/PHASE3_REPORT.md`).

## Verification, retries, loop handling, and consequential-action safety — findings

- **Delayed navigation is handled correctly only when the agent explicitly `wait`s for it**
  (`test_delayed_navigation_is_awaited_correctly`). Playwright's own actionability checks do
  *not* wait for a client-side `setTimeout`-driven navigation — this is a real, verified
  finding, not an assumption: the fixture site's delayed-nav page navigates 800ms after a
  click, and the action's own re-observation (immediately after the click) would not have
  seen it without a subsequent `wait(url_contains=...)` step. This is exactly why `wait` is
  a first-class action rather than something folded into `click`.
- **Wrong navigation is caught, not silently accepted**
  (`test_wrong_navigation_is_caught_by_verifier`): clicking a link that goes somewhere
  unexpected produces exactly one failed `VERIFICATION_RESULT` and escalates recovery to
  `retry` — proving the verifier doesn't rubber-stamp "the browser did something."
- **Idempotency is enforced by the agent's own policy, not by the target page.** The
  wizard-confirmation fixture (`tests/fixtures/simple_site/wizard_confirm.html`)
  deliberately does *not* disable its Submit button after one click — specifically so the
  test proves `agent/recovery.py`'s Case-C policy is what prevents a second submission, not
  an accident of the page's own UI state.

## Unresolved issues

- The idempotency check in `agent/loop.py::step()` only inspects the *immediately preceding*
  attempt at the same action fingerprint (`last_same`), not the full history — sufficient for
  every scenario tested here, but a model that alternates between two different failing
  actions before returning to a third repeat would not trigger the same-fingerprint check on
  the third attempt as early as a full-history scan would. The navigation-loop and
  repeated-action detectors provide a second line of defense for that broader pattern.
- `classify_risk()`'s keyword list is necessarily incomplete (any real deployment will find
  consequential actions it doesn't recognize by name). This is a known, documented tradeoff
  of "conservative and generic, not per-site" per the build brief — expanding it is Phase 4+
  work informed by real failures, not a Phase 1-3 gap to silently patch over.
