# BrowserAgent Master Status

Canonical handoff document. Written at the end of the "final integration pass"
(router fixes + Playwright CDP-attach persistent-browser mode + real UI validation),
branch `final/cdp-persistent-browser`, off `main` @ `c356b56` (tag `phase5b-pass`).
Read this instead of re-reading the whole project history.

## 1. Project Goal

A fully local, small-LLM-driven browser agent: Qwen3-8B (via Ollama) decides browser
actions from a compact page observation, with deterministic (non-LLM) safety, verification,
and crash-recovery machinery around it. On top of that core loop: a plain-English natural-
language router and local web UI so a normal user never has to think about "batch" vs.
"workflow" vs. "task" — they type what they want and BrowserAgent figures out whether it's a
single-site read/action, a multi-site sweep, an ordered sequence of cross-site actions
(with fact-passing between steps), or an open-web research task. Everything runs on the
user's own machine; nothing leaves it except calls to the local model and the websites the
task itself visits. As of this pass, the browser BrowserAgent drives can also be a
**persistent** one the user starts and logs into once, with BrowserAgent's own process
lifetime fully decoupled from it.

## 2. Current User Experience

```powershell
# one-time-ish: start a persistent, dedicated Chromium with remote debugging
browser-agent browser start

# start the UI attached to it
browser-agent ui --browser-mode cdp_attach --cdp-endpoint http://127.0.0.1:9222
```

Open `http://127.0.0.1:8765`. Type a task in plain English, e.g.:

- `Check these URLs and tell me which assignments I still have to do: https://a.edu https://b.edu`
- `Go to https://a.example.com and set the display mode to Compact. Then go to https://b.example.com and enable the weekly summary.`
- `Go to https://a.example.com and find the project code. Then go to https://b.example.com and enter that exact code in the matching field.`
- `Research college essay advice from a number of good sources and give me an evidence-backed report.`

For quick one-off testing without a separate browser window, `browser-agent ui` alone works
too (`launch` mode — BrowserAgent starts and owns its own throwaway Chromium, same as before
this pass). Ollama (`ollama serve`, with `qwen3:8b` pulled) must be running either way.

## 3. Hardware / Runtime

Proven Windows machine: RTX 4070, 12282 MiB / 12 GiB VRAM, driver supporting CUDA. Qwen3-8B
via Ollama, confirmed 100% GPU-resident (`ollama ps`), context window 8192. GPU inference:
p50 latency ~3.18s, p95 ~3.24s, ~71 tok/s generation throughput, 20/20 success with 0
retries in Phase 5's live benchmark. A CPU fallback path exists (works, ~9 tok/s, much
higher tail latency) but GPU is the validated normal path.

Mac/Apple Silicon: not validated in any phase to date (no report mentions it). Ollama itself
supports macOS, so it should work in principle, but BrowserAgent has no macOS-specific
validation evidence.

This pass's CDP-attach persistence test was run for real on this machine: real Chrome
(`Chrome/151.0.7922.174`), real Ollama + `qwen3:8b`, real fixture HTTP server. See Section 6
and Section 21.

## 4. Current Architecture

```
UI (ui/app.py, ui/static/index.html)
  -> router (router/policy.py: try_deterministic_route(), Qwen fallback route_with_model())
     -> RouterDecision: single_site | multisite_sweep | ordered_workflow | research
        -> AgentLoop (single_site)            agent/loop.py
        -> BatchOrchestrator (multisite_sweep) batch/orchestrator.py
        -> WorkflowOrchestrator (ordered_workflow) workflow/orchestrator.py
        -> discover_sources + BatchOrchestrator (research) research/discovery.py

AgentLoop.step(): observe -> decide (Qwen, schema-constrained) -> validate -> risk-gate
  -> approval (if consequential) -> execute -> re-observe -> verify -> event-log -> checkpoint

self.browser.<method>()  <- the ONLY way agent/loop.py touches a browser
  browser/playwright_backend.py::PlaywrightBackend
    mode="launch"     -> launch_persistent_context() (default; tests/fixtures)
    mode="cdp_attach" -> connect_over_cdp() to an already-running Chromium (new, this pass)
  browser/observer.py::extract_observation -> browser/page_model.py::PageObservation
  agent/verifier.py -- before/after PageObservation diff, no LLM involved
  memory/event_store.py -- append-only event log; memory/task_state.py -- replayed TaskState
```

`PlaywrightBackend` is the only module that ever touches a live Playwright `Page`. Element
identity is always re-resolved from the *current* observation right before acting — no
locator/handle is ever cached across steps, in either browser mode. This thin coupling (only
two call sites construct `PlaywrightBackend`: `agent/loop.py` and `research/discovery.py`)
is exactly what the BrowserOS feasibility research (Section 8 below) found and is why adding
`cdp_attach` mode required no new abstraction layer.

## 5. Phase History

| Phase | Goal | Result | Lesson |
|---|---|---|---|
| 1 | Prove Qwen3-8B can choose browser actions from a compact observation | 68/68 deterministic tests; no live-model validation yet (honestly reported as PARTIAL) | Defense-in-depth (grammar -> parse -> Pydantic -> semantic validation) built before answering "does the model actually work" |
| 1B | Fix model/tool action contract | 0/15 -> 15/15 smoke; holdout 5/5; 30/30 structured-output probe | Action-specific schemas + target-binding rules turned Qwen3-8B into a reliable executor (avg calls/task 8.40->4.47) |
| 2 | Deterministic verification/idempotency/recovery | 21 unit + 5/5 integration, real Playwright, no mocks | Idempotency must be enforced by agent policy, not page UI state |
| 3 | Event-sourced state, real crash recovery | 11 unit + 3/3 real-kill integration | `launch_persistent_context` restores cookies, not the live DOM — ambiguous post-crash actions correctly block rather than guess |
| 4 | Bounded long-horizon context + memory | Smoke 15/15, holdout 5/5, but long-horizon ablation mostly failed (PARTIAL) | Failure was facts not being *applied*, not token overflow; embeddings deferred as unjustified |
| 4B | Fix "present but not applied" facts | 98/98 deterministic; long-horizon 15/15 + 9/9 holdout; crash/resume 9/9; 54/54 facts applied | A deterministic constraint guard (not embeddings/bigger model) fixed application reliability |
| 5 | Durable multi-site batch orchestration | Assignment/research precision=recall=1.00 at 10-100 targets; crash/resume 3/3; 134/134 suite | Most "extraction failures" were infra/evaluator bugs, not model quality; sequential processing sufficient |
| 5B | NL router, local UI, ordered workflows, research | Router 39/39 + 15/15 live; workflow 2/4->4/4 after a fix, repeated 4/4 twice; 215 deterministic tests | The "unreliable" cross-site fact-passing was an interface bug (JSON-in-a-string), fixed via schema-validated fields |
| final (this pass) | Fix 2 router bugs from real-UI use; add persistent-browser (CDP attach) mode | See Section 6 | Both bugs were router-internal (target isolation, verb coverage), unrelated to backend choice; Playwright `connect_over_cdp` gives the persistence win with zero new dependency |

## 6. Proven Validation Results (this pass)

- **Router regression**: 39/39 pre-existing deterministic router tests still pass, plus 6
  new tests for the two fixed findings — 45/45 total (`tests/unit/test_router.py`).
- **Unit suite**: 186/186 passed (`tests/unit`).
- **Browser/CDP integration**: `tests/integration/test_cdp_attach.py` 6/6 passed (attach,
  reuse-existing-tab, disconnect-without-closing, reconnect, empty-browser page creation,
  launch-mode-unchanged regression guard).
- **No regression in existing Playwright-backed suites**: `test_phase1_browser_actions.py`,
  `test_phase2_verification_recovery.py`, `test_element_mapping.py`, `test_phase3_crash_recovery.py`,
  `test_phase1b_contract_repair.py`, `test_phase4_long_horizon.py` — all green after the
  `PlaywrightBackend` change (30 tests total across these files).
- **Real UI, launch mode** — both previously-failing scenarios now PASS end to end against
  the local fixture site, driven by real Qwen3-8B over Ollama:
  - **TEST A** (ordered action): router split into 2 isolated steps (`Set the display mode
    to Compact` / `Enable the weekly summary...`), both executed and independently
    `"verified": true`.
  - **TEST B** (cross-site fact pass): routed as `ordered_workflow` (was `multisite_sweep`
    before the fix); step 1 extracted `"facts": {"project_code": "AX-42"}`, step 2 entered
    it and verified.
- **Real CDP-attach persistence experiment** (see Section 21 for the full transcript):
  started a real dedicated Chromium (`browser-agent browser start`), manually opened a
  fixture page and set state on it, attached BrowserAgent via `cdp_attach`, ran a real read
  task, stopped BrowserAgent, confirmed Chrome and the exact same tab ID were still there,
  restarted BrowserAgent, reattached, confirmed the same tab, ran another read task
  successfully. Also ran the full TEST B ordered workflow over `cdp_attach` mode end to end
  — completed and verified, proving the router/workflow layer is backend-lifecycle-independent.
- **Known pre-existing gap, unrelated to this pass**: `tests/integration/test_ui_jobs.py`
  stalls under pytest on this Windows machine (documented issue, Section 15). Isolated:
  `test_ui_app.py` alone passes (9/9); `test_cdp_attach.py` alone passes (6/6); only
  `test_ui_jobs.py` hangs, even though it uses a fully mocked inference client (no network
  wait). Not caused by this pass's changes — its scenarios were instead validated by
  driving the real running UI server directly (Section 6 above, Section 21), which is a
  stronger signal than a mocked pytest run anyway.

## 7. Model

Qwen3-8B via Ollama (`qwen3:8b`), `temperature: 0.1`, `context_window: 8192`. Sufficient
because: schema-constrained JSON output (GBNF grammar + Pydantic validation) makes the
model's job "pick one valid action," not "write correct JSON from scratch," and Phase 1B/5B
both found reliability problems traced back to interface bugs, not model capacity. On the
RTX 4070: ~71 tok/s generation, ~3.2s p50 decision latency, 100% GPU-resident. No evidence
so far justifies a larger model.

## 8. Browser Layer

Two lifecycle modes in `browser/playwright_backend.py` (`browser.mode` in config):

- **`launch`** (default): `launch_persistent_context()` — BrowserAgent owns the browser
  process end to end. Used by all tests/fixtures/benchmarks; unchanged behavior from before
  this pass.
- **`cdp_attach`** (recommended for everyday use, new this pass): `connect_over_cdp()` to an
  already-running Chromium the user started separately
  (`browser-agent browser start`). BrowserAgent's process lifetime is fully decoupled from
  the browser's: `close()` disconnects the local Playwright driver only, never calling
  `browser.close()`/`context.close()`, so the browser process, its profile, and open tabs
  are always left exactly as they were.

Page-selection policy on attach (Section 10 of the task spec): reuse the most recently
opened/navigated non-blank tab if one exists (approximated via `context.pages` order, since
CDP doesn't expose true focus timestamps — documented limitation); if none, reuse the first
page found; if the browser has no pages at all, create exactly one in an existing context
rather than launching a separate browser. Never closes or reorders the user's tabs.

BrowserOS was researched (not adopted) as an alternative backend — see
[`docs/BROWSEROS_BACKEND_FEASIBILITY.md`](BROWSEROS_BACKEND_FEASIBILITY.md). Verdict:
`USE_PLAYWRIGHT_CDP_ATTACH`. BrowserOS's own docs state that closing an agent's session
closes its tabs with it — the opposite of the persistence property being sought — while
Playwright's own `connect_over_cdp()` already provides it, with zero new dependency, license
entanglement, or network-exposed control surface, and requires no change to
`PageObservation`, numeric target IDs, the verifier, event sourcing, or any existing test
fixture.

## 9. Persistence

- **Event sourcing** (`memory/event_store.py`): append-only log of `ACTION_INTENT`,
  `ACTION_RESULT`, `VERIFICATION_RESULT`, etc. `TaskState` (`memory/task_state.py`) is always
  a replay of this log, never held as the source of truth in memory.
- **Batch/workflow state**: `batch/store.py`, `workflow/store.py` — durable, resumable,
  survive process kill (proven by real `os._exit()` tests in Phase 3, and crash/resume gates
  in Phases 4B/5).
- **UI job state**: `runtime/ui/jobs.db` (`ui/store.py`) — prompt, router decision, status,
  final result; last 20 shown in the UI history list.
- **Browser session/profile persistence**: in `cdp_attach` mode, this is now also true of the
  browser itself — cookies, logins, and open tabs survive a BrowserAgent restart because
  BrowserAgent never owned that browser process to begin with (Section 6, Section 21).
- **What survives a BrowserAgent restart**: all of the above, plus — new in this pass — the
  browser session itself when running in `cdp_attach` mode.

## 10. Memory Architecture

Bounded tiered context (`agent/context_builder.py`, `agent/config.py::ContextConfig`):
recent-actions window + running summary + FTS5-retrieved memory + active facts, budgeted to
`max_total_tokens` (default 4096; observed max in practice ~1406 in Phase 4B's validation).
No embeddings — FTS5 keyword search plus a deterministic "constraint guard" (Phase 4B) that
corrects a model decision when it contradicts a verified high-confidence fact was sufficient
to fix the "fact present but not applied" failure mode. Embeddings remain explicitly
unjustified by evidence collected so far.

## 11. Multi-Site Architecture

`batch/orchestrator.py` (`BatchOrchestrator`) drives `multisite_sweep`/`research`: a durable
queue of work items, one child `AgentLoop`-equivalent run per target, a result ledger,
dedupe, and a schema-validated `ResultContract` for synthesis (`generic`/`assignment`/
`research`). `workflow/orchestrator.py` (`WorkflowOrchestrator`) drives `ordered_workflow`:
explicit ordinal steps (`WorkflowStepPlan`), each step's verified output facts persisted and
made available to later steps via `VERIFIED WORKFLOW INPUTS` context — this is what makes
cross-site fact-passing (TEST B) work. Sequential processing only; no parallelism
implemented (found unjustified in Phase 5).

## 12. Research Architecture

`research/discovery.py::discover_sources` — one bounded search round: open a search-results
(or listing) page, deterministically enumerate candidate links, ask the model to select
relevant ones **by id** (never by transcribing a URL), resolve ids back to real URLs, then
run the normal `BatchOrchestrator` sweep over them with the `research` result contract. This
still uses its own isolated `launch`-mode `PlaywrightBackend` with a dedicated profile
directory (not wired to `cdp_attach` in this pass — see Section 15). Known limitation
carried over from Phase 5B: real open-web search engines can trigger bot detection; only a
10-site public-web pilot has been run (6/10 clean).

## 13. Safety

- **Read-only default**: router's `infer_policy()` defaults to `read_only` unless action
  verbs are present in the prompt; this is only the *default* — real gating is
  `agent/schemas.py::classify_risk()`, independent of the router.
- **Scope enforcement**: `NavigationScope` (batch policy) constrains sweep/research
  navigation to same-origin by default.
  Approval: consequential (`CONSEQUENTIAL` risk) actions pause and require explicit
  Approve/Deny — via the UI's approval box (HTTP-driven callback) or CLI `input()` prompt.
  Denying doesn't crash the task; it's recorded and the task reports what it could/couldn't
  finish.
- **Manual login**: BrowserAgent never types a password. A detected login page pauses the
  task (`looks_like_login_page`) with an explicit "log in, then click Continue" UI state.
- **CDP security**: remote debugging is bound to `127.0.0.1` only; documented as never to be
  exposed to a LAN/public network, since an open CDP endpoint grants full control of the
  browser, including any authenticated sessions in it. The persistent profile
  (`browser-agent browser start --profile-dir`) is always a dedicated one, never the user's
  everyday Chrome profile.

## 14. UI

FastAPI app (`ui/app.py`) + single static page (`ui/static/index.html`), bound to
`127.0.0.1` only. Submit a prompt -> router decision -> live status via SSE (status dot +
activity line) -> approval box / login box as needed -> final result -> history list (last
20, stored in `runtime/ui/jobs.db`, clearable). New this pass: a small **Browser:
Connected**/**Not connected** indicator (`GET /api/browser/status`), shown only in
`cdp_attach` mode (launch mode has nothing external to check ahead of time) — not a
dashboard, just enough to tell the user whether their persistent browser needs starting.

## 15. Known Limitations

- Real open-web search engines may trigger bot detection during research source discovery
  (Phase 5B, unresolved, carried forward).
- Arbitrary real websites are not guaranteed to work — only fixture sites and a small (10)
  public-web pilot have been validated.
- No vision / screenshot-based understanding — text/accessibility extraction only; not yet
  justified by any encountered failure.
- No domain skills (compressed per-site navigation patterns) — not yet justified.
- No parallelism across batch/workflow items — sequential found sufficient through Phase 5's
  scale testing.
- `test_ui_jobs.py` stalls under pytest on this Windows machine (pre-existing, not
  introduced by this pass — confirmed isolated to that one file; `test_ui_app.py` and the
  new `test_cdp_attach.py` both pass cleanly alone). Root cause not fully diagnosed; worked
  around by validating those scenarios directly against the live UI server instead.
- `research/discovery.py` is not wired to `cdp_attach` mode — it always uses its own
  isolated `launch`-mode browser with a dedicated profile. This is deliberate: reusing the
  user's active attached tab for an ephemeral background search would risk navigating away
  from whatever the user is actually looking at.
- CDP page-selection "most recently active tab" is a `context.pages`-order approximation,
  not a true focus timestamp (CDP doesn't expose one) — a tab opened long ago but manually
  refocused most recently may not be detected as such.
- No macOS validation to date.

## 16. Things Explicitly NOT Needed Yet

Embeddings, a larger model, vision by default, parallel agents, BrowserOS, a raw-CDP backend
rewrite, a new memory architecture. All examined and found unjustified by evidence at
various phases (see Section 5) or, for BrowserOS, this pass's own research
(`docs/BROWSEROS_BACKEND_FEASIBILITY.md`).

## 17. Git State

- `main` @ `c356b56` (tag `phase5b-pass`) before this pass.
- `research/browseros-backend-feasibility` — pushed to origin, preserved, not merged into
  `main` (the feasibility study itself; its one file is also copied into this branch so
  in-repo doc links resolve — see Section 8).
- `final/cdp-persistent-browser` — this pass's branch, off `main`.
- Tags: `phase1b-pass`, `phase5b-pass`; `browseragent-v1-ready` created only if Section 30's
  gate fully passes (see the final response for this run's actual outcome).

## 18. Important Docs

- `ARCHITECTURE.md`
- `docs/PHASE1_REPORT.md`, `PHASE1B_REPORT.md`, `PHASE2_REPORT.md`, `PHASE3_REPORT.md`,
  `PHASE4_REPORT.md`, `PHASE4B_REPORT.md`, `PHASE5_REPORT.md`, `PHASE5B_REPORT.md`
- `docs/BROWSEROS_BACKEND_FEASIBILITY.md`
- `docs/USING_BROWSERAGENT.md`
- `docs/BROWSERAGENT_MASTER_STATUS.md` (this file)

## 19. How To Run Tests

```powershell
# deterministic unit suite
python -m pytest tests/unit -q

# router-specific
python -m pytest tests/unit/test_router.py -v

# browser/CDP integration (real Playwright, no Ollama needed)
python -m pytest tests/integration/test_cdp_attach.py -v
python -m pytest tests/integration/test_phase1_browser_actions.py tests/integration/test_phase2_verification_recovery.py tests/integration/test_phase3_crash_recovery.py tests/integration/test_phase1b_contract_repair.py tests/integration/test_phase4_long_horizon.py -q

# UI (test_ui_app.py only — see Section 15 for the test_ui_jobs.py caveat)
python -m pytest tests/integration/test_ui_app.py -q
```

Run integration test files individually rather than all together where possible — batching
unrelated Playwright/FastAPI integration files in one pytest invocation is where the Windows
stall in Section 15 has been observed to trigger; each file run alone has been green.

## 20. How To Use BrowserAgent Today

1. Start Ollama (`ollama serve`) with `qwen3:8b` pulled.
2. Start a persistent browser once: `browser-agent browser start` (finds Chrome
   automatically, launches it with a dedicated profile and `--remote-debugging-port=9222`).
3. Start the UI attached to it: `browser-agent ui --browser-mode cdp_attach --cdp-endpoint http://127.0.0.1:9222`.
4. Open `http://127.0.0.1:8765`.
5. Type a task in plain English, press Run.
6. If prompted, log in manually in the browser window, then click Continue.
7. If prompted, Approve or Deny a consequential action.
8. Read the final result in the UI (or check the history list later).

Core BrowserAgent is ready for real use; future work should be driven by failures
encountered in actual tasks, not another speculative phase.

## 21. Real CDP Persistence Experiment — Evidence Log

Run on this machine, this session, with real Chrome and real Ollama/Qwen3-8B (not mocked):

1. `browser-agent browser start --port 9222 --profile-dir ./runtime/browseragent_persistent_profile`
   -> launched `C:\Program Files\Google\Chrome\Application\chrome.exe`, confirmed
   `GET http://127.0.0.1:9222/json/version` returns `Chrome/151.0.7922.174`.
2. Manually opened a new tab at the local fixture site (`PUT /json/new?...workflow_site_a.html`)
   and manually set its `<select>` to "Compact" via a separate, throwaway Playwright
   connection — simulating a user's pre-existing session state. Tab id `F6091...`.
3. `browser-agent ui --browser-mode cdp_attach --cdp-endpoint http://127.0.0.1:9222` ->
   `GET /api/browser/status` returned `{"mode":"cdp_attach","connected":true}`.
4. Submitted `"Tell me what this page is about."` (no URL) — routed `single_site`, empty
   `targets` (i.e. "use whatever's already open"). Completed; the task operated on tab
   `F6091...` throughout (confirmed via `Page.navigate` history in the task's event log and
   `GET /json/list` before/after — the model followed a couple of in-page links but never
   opened a new tab).
5. Killed the `browser-agent ui` process. Confirmed via `GET /json/version` and
   `GET /json/list`: Chrome still running, tab `F6091...` still open at its last URL.
6. Restarted `browser-agent ui --browser-mode cdp_attach ...`. `GET /api/browser/status` ->
   connected again.
7. Submitted a second read task. Completed successfully; event log confirms it read tab
   `F6091...` again (same tab, same final URL as step 4 left it) — proving reattachment
   found the persisted session, not a fresh one.
8. Submitted the TEST B ordered workflow (cross-site fact pass) while still attached over
   `cdp_attach`. Completed: step 1 extracted the project code, step 2 entered and verified
   it — identical outcome to the `launch`-mode run in Section 6, and `GET /json/list`
   afterward showed no new tabs were created (still exactly the 2 tabs from step 2).

Conclusion: browser process survival, tab survival, disconnect-without-closing, and
reconnect-to-the-same-session all confirmed with real evidence, not simulated.
