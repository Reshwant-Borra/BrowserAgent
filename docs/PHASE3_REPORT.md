# Phase 3 Report — Event-Sourced State and Crash Recovery

## Verdict: **PASS**

All exit-gate items pass, including a real out-of-process kill test. One genuine
architectural finding surfaced during implementation (not anticipated in
`docs/IMPLEMENTATION_PLAN_PHASES_1_3.md`) and was fixed rather than worked around — see
"Finding: persistent context does not restore live page state" below.

## What was built

- `memory/schema.sql` — `tasks` / `events` / `task_state`, exactly as specified in the build
  brief, WAL mode enabled.
- `memory/event_store.py` — append-only `events` (source of truth), synchronous stdlib
  `sqlite3` (see "Architecture deviation" below for why not `aiosqlite`).
- `memory/replay.py` — `replay_task()` folds the event log into a `TaskState`, including
  detecting a dangling `ACTION_INTENT` with no matching `ACTION_RESULT` (the crash window).
- `memory/task_state.py` — `TaskStateStore.load()` never trusts the persisted `task_state`
  row blindly: it compares the row's `last_event_id` against the true max event id and
  rebuilds via replay on any mismatch, including "row doesn't exist."
- `agent/loop.py::_reconcile_pending_intent` — the resume-time atomicity check: an
  interrupted action is never blindly replayed. It is actively reconciled against a fresh
  observation, and resolved one of three ways depending on risk and what could be confirmed
  (see findings below).
- `tests/integration/_kill_test_worker.py` + `test_phase3_crash_recovery.py` — a real
  subprocess is launched, executes a real browser action, and is killed via `os._exit()`
  (bypassing all Python cleanup — no `finally`, no atexit) at a precise, reproducible point
  immediately after the action's real browser-level side effect but before its
  `ACTION_RESULT`/`VERIFICATION_RESULT` events are persisted. A second, independent
  subprocess then resumes the same `task_id` from the same on-disk database and browser
  profile directory.

## Test results

```
tests/unit (memory/replay.py, memory/task_state.py rebuild-from-events)   11 passed
tests/integration/test_phase3_crash_recovery.py                           3 passed
```

Exit-gate checklist:

1. **Events are append-only** — PASS. Nothing in the codebase issues `UPDATE`/`DELETE`
   against `events`; `EventStore.append()` is the only write path.
2. **task_state is rebuildable from events** — PASS, and proven three ways in
   `tests/unit/test_task_state_store.py`: row never written, row present but stale (an event
   appended after the last save), and row deleted out from under a live store — all three
   correctly rebuild via `replay_task`.
3. **Process can be terminated mid-task** — PASS. `_kill_test_worker.py` really is a separate
   OS process, really executes a real Playwright click, and really dies via `os._exit(137)`
   with no cleanup.
4. **Task can resume** — PASS. A second, independent subprocess loads the same `task_id`.
5. **Ambiguous interrupted actions do not blindly replay** — PASS, for both scenarios tested
   (see findings).
6. **Browser sessions can persist where possible** — PASS for cookies/storage (Playwright
   `launch_persistent_context`); **explicitly does NOT persist the live open page/DOM** — see
   the finding below, this is a real platform constraint, not a gap in this implementation.
7. **Repeated resume does not corrupt state** — PASS:
   `test_repeated_resume_of_completed_task_is_a_noop` resumes an already-completed task and
   confirms zero new events are appended (the loop sees `status != "running"` and returns
   immediately, per `AgentLoop.run()`).
8. **All Phase 1 and Phase 2 tests still pass** — PASS, see combined tally below.

## Finding: persistent context does not restore live page state

While building the kill test, the first version assumed that after a crash and resume, a
fresh observation of "wherever the persistent-context browser currently is" would show the
same page the crashed process had open (since cookies/localStorage persist). **This is
false.** Playwright's `launch_persistent_context` persists cookies, local storage, and
session storage to disk, but a fresh launch always starts on a blank page — it does not
reopen the previously-active tab or restore in-memory DOM/JS state. The original kill test
failed for exactly this reason (the reconciliation observation was of a blank page, not the
page the crash happened on).

**Fix applied** (`agent/loop.py::_reconcile_pending_intent`): before judging whether a
pending action succeeded, the agent now re-navigates first — to the pending intent's own
destination URL if the action was `open_url` (safe: a GET is idempotent, re-visiting it to
*inspect* state is not the same as *retrying* a consequential action), or to
`state.current_url` (the last URL actually observed before the crash) otherwise.

**This does not fully solve the underlying problem for same-page, client-side-only
mutations with no server/URL/storage record** — and that limitation is real, not a bug to
paper over. It is captured directly in the two kill-test scenarios:

- **`test_kill_after_readonly_navigation_reconciles_and_completes`**: the crash happens right
  after an `open_url` navigation. Re-visiting that same URL (idempotent) genuinely confirms
  the destination page's content matches `expected_result`. Reconciliation succeeds, the task
  resumes as `running`, and completes normally in one further step. **Verified: exactly one
  `ACTION_INTENT` event exists for that fingerprint — the original intent is resolved, not
  duplicated by the reconciliation re-visit.**
- **`test_kill_mid_action_and_resume`**: the crash happens right after a real click on a
  CONSEQUENTIAL "Submit Application" button whose only observable effect is an in-memory
  JS-driven text change on the *same* page (no navigation, no cookie, no server round trip —
  intentionally, to model the worst case). After resume, re-navigating to
  `state.current_url` reloads that page **fresh**, which necessarily resets its in-memory JS
  state — there is no way to distinguish "submitted, then crashed" from "never submitted" by
  re-observation alone in this case, for any browser-automation architecture, because the
  only record of the mutation lived in a JS heap that no longer exists. **The system's
  correct behavior here is to refuse to guess**: risk is CONSEQUENTIAL, verification of the
  reconciliation attempt fails, so the task is marked `blocked` with reason "ambiguous
  consequential action after crash; needs human review" — never silently resubmitted. This
  is verified as the actual, intended outcome (not accepted as a failure): the test asserts
  `status == "blocked"` and exactly one `ACTION_RESULT` for that fingerprint (recorded during
  reconciliation, not a duplicate live click).

Both outcomes are correct; the second is a hard limit of what re-observation can ever prove,
not something a smarter recovery policy could fix. Real target sites where a submit causes a
URL change or server-persisted state (most real forms) would fall into the first,
resolvable, category — this fixture was deliberately built to also test the worst case.

## SQLite durability

WAL mode is confirmed active (`PRAGMA journal_mode` returns `wal`, checked directly against
a live `EventStore` connection, not assumed). Every `EventStore.append()` and
`TaskStateStore.save()` call commits immediately — there is no batching/deferred-commit path
that could lose an event that was already returned to the caller as persisted. This was not
independently stress-tested beyond the kill test itself (e.g., no fsync-vs-page-cache
power-loss simulation) — WAL + per-write commit is standard practice for this durability
level, but "we did not additionally simulate a raw power failure" is stated here rather than
implied as fully covered.

## Browser session persistence

Each task gets its own profile directory (`runtime/tasks/<task_id>/browser_profile`) — a
deliberate choice over one shared profile, specifically so two tasks never contend for
Playwright's persistent-context profile lock. Cookies and storage survive a restart, per
Playwright's documented persistent-context behavior; the open page/tab does not (see finding
above) and the resume path now compensates for that.

## Combined test tally (Phases 1-3 together, `pytest -m "not model"`)

```
tests/unit           55 passed
tests/integration     13 passed
------------------------------
TOTAL (deterministic) 68 passed, 0 failed

tests/model            1 skipped (no live llama.cpp server in this environment)
```

## Architecture deviations from the plan

- **`aiosqlite` was not used; stdlib `sqlite3` was, synchronously.** The implementation plan
  listed both as options. Justification: every call is a small local-disk operation, there is
  exactly one writer (the current process, one task at a time), and WAL mode already
  provides the durability/concurrency properties needed — an async driver would add a
  dependency without a measurable correctness or throughput benefit at this scale. Documented
  in `memory/event_store.py`'s module docstring, not a silent choice.
- **The resume path gained a re-navigation step not explicitly specified in the plan** (see
  "Finding" above) — added because the kill test surfaced a real correctness gap during
  implementation, exactly the kind of "implementation reveals a concrete contradiction"
  case the build brief said should override the original plan.
