# Implementation Plan — Phases 1–3

Scope: prove the foundation from `ARCHITECTURE.md` — compact-observation decision loop, deterministic verification/recovery, event-sourced crash-safe state — without hybrid retrieval, embeddings, vision, page deltas, skill learning, or planner/model swapping. Those are explicitly out of scope for this round.

## Modules

```
config/default.yaml          runtime config (model endpoint, browser, context budgets, recovery thresholds, storage paths)

agent/schemas.py             ActionType, ModelDecision, ExpectedResult, VerificationResult/CheckResult,
                              RiskLevel + classify_risk(), RecoveryLevel — all Pydantic
agent/context_builder.py     deterministic prompt assembly (static → semi-stable → volatile ordering)
agent/decision.py            calls inference client, parses+validates (GBNF → JSON → Pydantic → semantic checks),
                              rejects stale targets/type-mismatches before execution
agent/verifier.py            expected_result vs actual PageObservation → VerificationResult (pure function, no LLM)
agent/recovery.py            NORMAL→RETRY→REFRESH_STATE→DEEP_RECOVERY→REPLAN_REQUIRED→USER_REQUIRED state machine
                              + idempotency policy (Case A/B/C from ARCHITECTURE.md §14)
agent/loop_detector.py       no-op / repeated-action / navigation-loop / stale-target / modal-obstruction detectors
agent/loop.py                the actual single-step loop tying the above together; instrumented (metrics)

browser/playwright_backend.py  persistent-context Playwright wrapper; one method per action verb
browser/observer.py            single page.evaluate() extraction pass → raw element/text data
browser/page_model.py          ElementRef (id + role/name/state + selector_hint, never a cached handle),
                                PageObservation (compact render + state_hash + size metrics)
browser/state_hash.py          normalized fingerprint: url + title + ordered (role,name,state) tuples + visible-text sample

inference/llama_client.py    httpx client for llama.cpp's native /completion endpoint (grammar= field for GBNF),
                              clean diagnostic on connection failure, captures llama.cpp's own timings block
inference/prompt.py           renders the ordered prompt sections + the tiny separate replan-prompt template
inference/grammar/action.gbnf GBNF constraining the outer JSON shape + action enum + confidence range + capped
                               optional reason string; params/expected_result are generic JSON objects — precise
                               per-action typing is enforced by Pydantic (layer 3), not the grammar (layer 1)

memory/schema.sql             tasks / events / task_state tables (per ARCHITECTURE.md §17, adopting the exact DDL
                               given in the build brief)
memory/event_store.py         append-only writer + reader; WAL mode; ACTION_INTENT/ACTION_RESULT split for atomicity
memory/task_state.py          derived-view read/update; fast path reads the row, falls back to replay on mismatch
memory/replay.py              replay_task(task_id) folds events → TaskState, used by task_state's consistency check
                               and by the resume path

cli/main.py                   `run "<goal>"`, `resume <task_id>`, `status <task_id>` (argparse; stdlib only)
```

No LangChain/LangGraph/AutoGen/CrewAI/vector DBs/Redis/Postgres. Playwright's persistent context (`launch_persistent_context`) is used directly, per-task profile directory (`runtime/tasks/<task_id>/browser_profile`) so tasks don't lock a shared profile.

## Data flow (one step)

```
observer.extract(page)                         # one page.evaluate() round trip
  → page_model.build(raw) → PageObservation (state_hash computed)
  → context_builder.build(task_state, observation, recovery_level)
  → llama_client.complete(prompt, grammar)      # GBNF-constrained
  → decision.parse_and_validate(raw) → ModelDecision | ValidationError
  → recovery.classify_risk(decision) → RiskLevel
  → [if CONSEQUENTIAL] approval gate (CLI prompt or block)
  → event_store.append(ACTION_INTENT)           # durability point before side effect
  → playwright_backend.execute(decision)         # the only side-effecting step
  → event_store.append(ACTION_RESULT)
  → observer.extract(page) → new PageObservation
  → verifier.check(decision.expected_result, new_observation) → VerificationResult
  → event_store.append(VERIFICATION_RESULT)
  → loop_detector.check(recent events) → repeated/no-op/nav-loop/modal signal
  → recovery.next_level(current_level, verification, loop_signal) → RecoveryLevel
  → task_state.update(...) ; commit                # checkpoint
```

Nothing here depends on the LLM remembering anything: every field `context_builder` needs is re-derived from `task_state` + a fresh `PageObservation`, never from a retained chat object. KV-cache reuse (stable prefix ordering in `context_builder`) is a `llama_client`-level performance detail, invisible to correctness.

## Dependencies

```
python 3.11+, playwright, pydantic v2, httpx, PyYAML, pytest, pytest-asyncio
stdlib: sqlite3, argparse, hashlib, json, logging, http.server (test fixture site only)
```

## Test fixture site

Static HTML/JS pages served by stdlib `http.server` (a `ThreadingHTTPServer` pytest fixture, not a framework) — covers: links/buttons/textbox/select/heading/hidden/disabled elements; a delayed-navigation button (client-side `setTimeout`); a no-op button; a wrong-navigation button; an in-page modal (no native `window.*` dialogs — those block automation and are explicitly avoided); two pages that redirect into each other (navigation-loop fixture); a download link; a multi-field form; and a "submit" button that visibly disables itself after first click (consequential-retry fixture).

## Tests

- `tests/unit/` — observation extraction (fixture HTML → expected `PageObservation`), element-ID→locator mapping, schema parsing (valid/invalid JSON, bad enum, missing target, wrong target type, out-of-range confidence), verifier logic, state-hash stability/sensitivity, loop-detector logic, recovery state-machine transitions, event replay correctness — all pure/mocked, no browser or model required.
- `tests/integration/` — real Playwright against the fixture site: full click/type/select/scroll/back/extract/download flows; Phase 2 scenarios (no-op detected, delayed nav verified correctly, wrong-nav caught, redirect-loop caught, consequential submit not double-fired); Phase 3 kill-test (subprocess started, killed mid-task, resumed, event log intact, no duplicated consequential action).
- `tests/model/` (pytest marker `model`) — the actual Phase 1 smoke test that invokes a configured local llama.cpp server; skipped with a clear message if the endpoint isn't reachable, never mocked.

`pytest -m "not model"` runs everything deterministic; `pytest -m model` requires a live server.

## Exit gates

**Phase 1:** endpoint integration works (or fails with a clean diagnostic); malformed/invalid model output never reaches Playwright; compact extraction matches fixtures exactly; element IDs map to correct locators; all nine action verbs work against the fixture site; the loop completes at least one multi-step fixture task end-to-end; unit + integration tests green.

**Phase 2:** every action carries `expected_result`; verifier is deterministic and covered by tests; no-op / repeated-action / navigation-loop detectors each have a passing fixture test; consequential actions cannot be auto-retried after an ambiguous state change; recovery transitions are unit-tested; Phase 1 tests still green.

**Phase 3:** events are append-only and never mutated; `task_state` is provably reconstructable via `replay_task` (a test deletes/corrupts the row and asserts replay recovers it); a real kill-and-resume integration test passes; an ACTION_INTENT with no following ACTION_RESULT is detected on resume and never blindly re-executed; browser profile persists across a restart; Phase 1+2 tests still green.
