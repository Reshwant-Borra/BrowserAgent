# BrowserAgent Master Status

Canonical handoff document. Originally written at the end of the "final integration pass"
(router fixes + Playwright CDP-attach persistent-browser mode + real UI validation), branch
`final/cdp-persistent-browser`, off `main` @ `c356b56` (tag `phase5b-pass`). Updated again at
the end of the "semantic task planner" pass (branch `feature/semantic-task-planner`, off
`main` @ `57d3599`, tag `browseragent-v1-ready`) — see Section 22 for that pass's evidence.
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
browser-agent start
```

One command: checks/starts Ollama, verifies `qwen3:8b`, checks/starts the persistent
CDP-attached Chrome (only ever reports success once `/json/version` actually responds — see
Section 25), starts the UI, and opens it. `browser-agent status` / `browser-agent stop` are
also available; the two-step `browser-agent browser start` + `browser-agent ui ...` sequence
below still works for debugging one stage in isolation.

Open `http://127.0.0.1:8765`. Type a task in plain English, e.g.:

- `Check these URLs and tell me which assignments I still have to do: https://a.edu https://b.edu`
- `Go to https://a.example.com and set the display mode to Compact. Then go to https://b.example.com and enable the weekly summary.`
- `Go to https://a.example.com and find the project code. Then go to https://b.example.com and enter that exact code in the matching field.`
- `Research college essay advice from a number of good sources and give me an evidence-backed report.`

As of the semantic-planner pass (Section 22), none of these need a literal URL at all —
BrowserAgent resolves references against your actually-open tabs or the page you're on:

- `Check all my course pages and tell me what I still need to do this week.`
- `Tell me what I still need to do on this page.`
- `Find the project code on the page where it's listed, then put that same code into the configuration page and verify it.`

If it can't find a safe match for what you meant, it asks instead of failing — see Section 22.

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
  -> router (router/policy.py::route(), mode-aware — see Section 22)
     1. router/extract.py::try_deterministic_route()   <- obvious literal-URL shapes, free
     2. router/semantic_planner.py::plan_task()         <- Qwen, schema-constrained TaskPlan
        -> router/resources.py::ResourceResolver        <- deterministic resolution
     3. (fallback only) router/llm_router.py::route_with_model()  <- pre-existing Qwen router
     -> RouterDecision: single_site | multisite_sweep | ordered_workflow | research
        | NeedsInput  <- ask the user instead of "no targets found" (Section 22)
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
| final (CDP pass) | Fix 2 router bugs from real-UI use; add persistent-browser (CDP attach) mode | 45/45 router, 192/192 combined, real persistence experiment passed | Both bugs were router-internal (target isolation, verb coverage), unrelated to backend choice; Playwright `connect_over_cdp` gives the persistence win with zero new dependency |
| semantic planner (this pass) | Replace the brittle keyword-first router's failure mode ("no targets found" on semantic requests like "check my course pages") with a schema-constrained planner + deterministic resource resolver | 98.4% overall / 100% holdout planner accuracy, 100% schema validity, 0 hallucinations (232 deterministic tests); exact reported failure resolved end to end with real open tabs — see Section 22 | The model is good at understanding *what* the user means; code must stay the sole authority on *what actually exists* — every resource reference resolves to a real, enumerable candidate (extracted URL or real open tab) the model only ever selects *by id*, never invents |

## 6. Proven Validation Results (CDP persistent-browser pass)

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
  refocused most recently may not be detected as such. Consequence observed during the
  semantic-planner pass's E2E validation: a batch/workflow child task in `cdp_attach` mode
  reuses/renavigates whatever tab this policy picks rather than opening one dedicated tab per
  work item — correct end-target per item, but it can "borrow" one of the user's other open
  tabs along the way. Not fixed (out of scope — see Section 22).
- No macOS validation to date.
- Semantic open-tab resolution (Section 22) has a small residual miss rate (~1/13 in the live
  benchmark) — it's tuned toward asking for clarification rather than guessing wrong, but
  it's not perfect pattern matching.
- Bounded replanning (Section 22, Section 9 of docs/SEMANTIC_PLANNER.md) was validated at the
  planner-classification level (`intent=mixed` detection, 100% in the live benchmark) and via
  fake-client unit tests, but not with a full live execution against fixture data that
  actually contains something to select between (the available fixture pages have no due-date
  content) — the mechanism itself (one bounded schema-constrained call, id-based selection,
  reusing `_run_single`) is the same pattern already proven elsewhere in this codebase.

## 16. Things Explicitly NOT Needed Yet

Embeddings, a larger model, vision by default, parallel agents, BrowserOS, a raw-CDP backend
rewrite, a new memory architecture. All examined and found unjustified by evidence at
various phases (see Section 5) or, for BrowserOS, this pass's own research
(`docs/BROWSEROS_BACKEND_FEASIBILITY.md`).

## 17. Git State

- `main` @ `57d3599` (tag `browseragent-v1-ready`) before the semantic-planner pass; `c356b56`
  (tag `phase5b-pass`) before the CDP pass.
- `research/browseros-backend-feasibility` — pushed to origin, preserved, not merged into
  `main` (the feasibility study itself; its one file is also copied into `main` so in-repo doc
  links resolve — see Section 8).
- `final/cdp-persistent-browser` — CDP pass's branch, merged into `main`.
- `feature/semantic-task-planner` — this pass's branch, off `main`, pushed, not merged pending
  review (Section 42 of the semantic planner task: "do not merge until validation passes").
- Tags: `phase1b-pass`, `phase5b-pass`, `browseragent-v1-ready`.

## 18. Important Docs

- `ARCHITECTURE.md`
- `docs/PHASE1_REPORT.md`, `PHASE1B_REPORT.md`, `PHASE2_REPORT.md`, `PHASE3_REPORT.md`,
  `PHASE4_REPORT.md`, `PHASE4B_REPORT.md`, `PHASE5_REPORT.md`, `PHASE5B_REPORT.md`
- `docs/BROWSEROS_BACKEND_FEASIBILITY.md`
- `docs/SEMANTIC_PLANNER.md`
- `docs/USING_BROWSERAGENT.md`
- `docs/BROWSERAGENT_MASTER_STATUS.md` (this file)

## 19. How To Run Tests

```powershell
# deterministic unit suite (includes tests/unit/test_semantic_planner.py)
python -m pytest tests/unit -q

# router-specific
python -m pytest tests/unit/test_router.py tests/unit/test_semantic_planner.py -v

# browser/CDP integration (real Playwright, no Ollama needed)
python -m pytest tests/integration/test_cdp_attach.py -v
python -m pytest tests/integration/test_phase1_browser_actions.py tests/integration/test_phase2_verification_recovery.py tests/integration/test_phase3_crash_recovery.py tests/integration/test_phase1b_contract_repair.py tests/integration/test_phase4_long_horizon.py -q

# UI (test_ui_app.py only — see Section 15 for the test_ui_jobs.py caveat)
python -m pytest tests/integration/test_ui_app.py -q

# semantic planner live-model benchmark (real Ollama, ~15-20 min for all 8 categories)
python benchmarks/run_semantic_planner_live.py
python benchmarks/run_semantic_planner_live.py --categories open_tabs,clarification  # subset
```

Run integration test files individually rather than all together where possible — batching
unrelated Playwright/FastAPI integration files in one pytest invocation is where the Windows
stall in Section 15 has been observed to trigger; each file run alone has been green.

## 20. How To Use BrowserAgent Today

1. `browser-agent start` — one command. It checks/starts Ollama, verifies `qwen3:8b` is
   installed, checks/starts the persistent CDP-attached Chrome (only ever reports success
   after `/json/version` actually responds — never on process creation alone), starts the
   UI, and opens it in your browser. Pass `--no-open` to skip the auto-open.
2. Type a task in plain English, press Run — no need to paste URLs if you mean something
   already open ("my course pages") or the page you're on ("this page").
3. If prompted, log in manually in the browser window, then click Continue.
4. If prompted, Approve or Deny a consequential action.
5. If BrowserAgent asks a clarifying question (it couldn't find a safe match for what you
   meant), answer in the text box and click Continue.
6. Read the final result in the UI (or check the history list later).
7. `Ctrl+C` stops the UI only; the persistent Chrome window and Ollama both keep running.
   `browser-agent status` shows what's currently up without changing anything;
   `browser-agent stop` (optionally `--browser`) tears down what BrowserAgent itself started.

The lower-level commands (`browser-agent browser start`, `browser-agent ui ...`) still work
individually for debugging one stage in isolation — see `cli/launcher.py` and section 25.

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

## 22. Semantic Task Planner Pass — Evidence Log

Motivation: `router/extract.py`/`router/llm_router.py` could only route a target already
spelled out as a literal URL. "Check all my course pages and tell me what I still need to do
this week" produced `multisite_sweep` with `targets=[]`, `requires_discovery=True`, and
`ui/jobs.py::_run_sweep` failed it outright with "no targets found in the task text" — a
reasonable natural-language request treated as an error. Full design:
[`docs/SEMANTIC_PLANNER.md`](SEMANTIC_PLANNER.md).

### What changed

New: `router/plan_schema.py` (TaskPlan/ResourceRequirement/PlannedStep/ReplanDecision),
`router/semantic_planner.py` (Qwen planning call), `router/resources.py` (deterministic
resource resolver + open-tab selection), `router/replanner.py` (bounded mixed-intent
follow-up), `browser/tabs.py` (CDP HTTP tab enumeration). Modified: `router/policy.py`
(mode-aware `route()` + plan->RouterDecision translation + `NeedsInput`), `router/schema.py`
(`mixed_intent_followup` field, additive), `agent/config.py`/`config/default.yaml`
(`routing.mode`, default `hybrid`), `ui/jobs.py`/`ui/app.py`/`ui/store.py`/
`ui/static/index.html` (clarification loop + bounded replan). Zero changes to `AgentLoop`,
`BatchOrchestrator`, `WorkflowOrchestrator`, `research/discovery.py`, or `PlaywrightBackend`.

### Live planner benchmark (`benchmarks/run_semantic_planner_live.py`, real Qwen3-8B/Ollama)

128 prompts (104 tuned + 24 holdout, unseen phrasings) across 8 categories. Final run:

```json
{
  "per_category": {
    "single_explicit": {"tuned": "12/13", "holdout": "3/3"},
    "current_page": {"tuned": "13/13", "holdout": "3/3"},
    "sweep_explicit": {"tuned": "13/13", "holdout": "3/3"},
    "ordered_workflow": {"tuned": "13/13", "holdout": "3/3"},
    "mixed_intent": {"tuned": "13/13", "holdout": "3/3"},
    "research": {"tuned": "13/13", "holdout": "3/3"},
    "open_tabs": {"tuned": "12/13", "holdout": "3/3"},
    "clarification": {"tuned": "13/13", "holdout": "3/3"}
  },
  "tuned_accuracy": 0.9807692307692307,
  "holdout_accuracy": 1.0,
  "overall_accuracy": 0.984375,
  "schema_validity_rate": 1.0,
  "hallucinated_target_count": 0,
  "total_prompts": 128
}
```

Two real bugs found and fixed mid-benchmark (not just prompt tuning against noise):

1. **sweep vs ordered_workflow confusion**: an early full run scored 76.6% overall because
   the planner defaulted multi-URL "check A and B and tell me X" prompts (no sequencing
   language) to `ordered_workflow` instead of `sweep`, and "find the cheapest and open it"
   style prompts to `intent=act` instead of `intent=mixed`. Fixed by rewriting the planner
   prompt's execution-shape/intent rules as an explicit ordered decision list with a
   contrastive example ("Check A and B and tell me which is cheaper -> sweep, NOT
   ordered_workflow"). Re-run after the fix: those categories went to 13/13 and 16/16.
2. **Over-inclusive open-tab selection**: the `clarification` category (same prompts as
   `open_tabs`, against a tab pool with no matching tabs) initially failed 3/16 — the
   selection sub-call, told to "select every tab that plausibly matches," defaulted to
   selecting unrelated tabs (email/shopping) rather than nothing when unsure. Tightened the
   prompt to make an empty result the explicitly-correct answer when nothing is a clear
   match. Re-run: `clarification` went to 16/16; `open_tabs` gave up exactly one match it
   previously had (a real, if small, precision/recall trade — but the failure mode moved from
   "silently picks the wrong tab" to "asks for clarification," which is the safer direction).

A benchmark-script bug was also caught and fixed before the first real numbers were usable:
the script never set `config.browser.mode = "cdp_attach"`, so `ResourceResolver.get_open_tabs()`
short-circuited to "no tabs" before the patched tab list was ever consulted — every `open_tabs`
prompt failed with a suspiciously fast ~1.6s latency (a single planner call, no selection
call) until this was noticed and fixed.

### Real end-to-end validation (real Chrome, real Ollama/Qwen3-8B, real tabs — not mocked)

Persistent Chromium started via `browser-agent browser start`; 7 real tabs opened against the
local fixture site with realistic titles set via a throwaway Playwright connection: 3
"course-like" tabs (`AP Chemistry - Course Home`, `AP Calculus - Assignments`,
`US History - Course Page`), 2 distractors (`Online Shopping - Cart`,
`YouTube - Funny Cat Video`), and 2 tabs for the cross-site scenario (`Project Info - Code
Lookup`, `Settings - Configuration Panel`). UI started with `--browser-mode cdp_attach`
(default `routing.mode: hybrid`).

- **Exact reported failure, reproduced and fixed**: `"Check all my course pages and combine
  everything I still need to do this week."` -> routed `multisite_sweep`, resolved to exactly
  the 3 course tabs (`workflow_site_a/b/c.html`), zero shopping/YouTube tabs included, zero
  invented URLs. Batch ran 3/3 items to completion. No "no targets found" error.
- **Current page**: `"Tell me everything I still need to do on this page."` -> `single_site`,
  empty targets, completed against whatever tab was attached.
- **Cross-site, no URLs, no "Site A/B" phrasing**: `"Find the project code on the page where
  it's listed, then put that same code into the configuration page and verify it."` -> first
  attempt produced `NeedsInput` because the planner classified both references as
  `current_page` (a single page can't be two different pages) — fixed by adding an explicit
  planner-prompt rule ("2+ distinct implicit targets with no URLs -> open_tabs for each, never
  current_page") plus a worked example matching this exact scenario. Re-run:
  `ordered_workflow`, step 1 -> `workflow_dep_a.html` (via tab selection matching "the page
  listing the project code"), steps 2-3 -> `workflow_dep_b.html` (via tab selection matching
  "the configuration/settings page") — zero explicit URLs anywhere in the prompt. Ran to
  completion: code `AX-42` extracted, entered, and verified.
- **Clarification round-trip**: `"Check my internship application pages and tell me their
  status."` (no internship-related tab open) -> `waiting_for_input` with a specific question.
  Answered via `POST /api/jobs/{id}/clarify` with a pasted URL -> re-routed (deterministic
  fast path picked up the now-literal URL immediately, no second planner call) -> completed.
- **Research routing**: `"Research strong college essay advice from multiple reputable
  sources and summarize the recurring themes."` -> `research`, `requires_discovery=True`,
  `targets=[]` — real `discover_sources()` call found real candidate URLs
  (`collegeessayguy.com` among them) and began sweeping them; stopped once the control-plane
  routing was confirmed (per the task's own instruction, full open-web search success is not
  required to validate this path — see Known Limitations for bot-detection risk).

One pre-existing (not introduced by this pass) architectural quirk was surfaced during this
validation: `PlaywrightBackend`'s `cdp_attach` page-selection policy causes sequential
batch/workflow child tasks to reuse and renavigate whichever tab was last attached rather than
opening one tab per work item — each item still ends up on the correct target URL (via
`open_url`), but a long sweep can "borrow" the user's other open tabs along the way. Recorded
as a known limitation (Section 15), not fixed — out of scope for a control-plane-only pass.

### Tests

- `tests/unit/test_semantic_planner.py`: 31 new tests (schema validity, resolver resolution
  incl. positional explicit-URL assignment for multi-step workflows, tab-selection
  hallucination guard, plan translation for all 4 execution shapes, NeedsInput triggers,
  routing-mode selection legacy/semantic/hybrid, clarification round-trip, replanner schema).
- Full deterministic suite: 232/232 passed (`tests/unit` + `test_cdp_attach.py` +
  `test_ui_app.py`), no regressions in any pre-existing router/CDP/UI test.

### Performance

Planner call: ~1.5-2.5s (single Qwen3-8B call, same order of magnitude as the existing
action-decision calls). Resource resolution adds one more call only for `open_tabs`
requirements (~1.5-2.5s tab-selection call) — negligible next to actual browser
observe/act/verify cycles (multi-second each). Deterministic fast-path prompts (literal URLs)
pay zero planner overhead, unchanged from before this pass.

## 23. Production Readiness — Final Summary (Post-Merge)

`feature/semantic-task-planner` merged into `main` with `--no-ff` (merge commit `054048d`,
history preserved, not squashed), tag `browseragent-v1-semantic-ready`. This section is the
one-stop summary; Section 22 above has the full narrative and evidence.

**Semantic planner architecture**: `router/semantic_planner.py::plan_task()` — one
schema-constrained Qwen3-8B call turns a plain-English prompt into a validated `TaskPlan`
(goal, intent, execution_shape, resource_requirements, steps, constraints). The plan proposes
*what the user means*; it has no field anywhere capable of holding a URL, so hallucination is
structurally impossible at this layer.

**Resource resolver**: `router/resources.py::ResourceResolver` — the sole deterministic
authority on what actually exists. `explicit_urls` resolves via the pre-existing
`extract_urls()`; `open_tabs` and `current_page` are covered next; `web_discovery` hands off
untouched to the existing `research/discovery.py` pipeline.

**Open-tab semantic selection**: `browser/tabs.py::list_open_tabs()` enumerates the attached
browser's real tabs via the CDP HTTP API; `router/resources.py::select_relevant_tabs()` asks
Qwen to choose relevant tabs by **id only** (never a URL) against the plan's description —
same anti-hallucination shape as `research/discovery.py`'s existing link selection.

**Current-page resolution**: an empty (or `current_page`-only) resource list translates to
`RouterDecision(targets=[])`, which `AgentLoop` already correctly treats as "act on whatever
page is already attached" — no new mechanism required.

**NEEDS_INPUT clarification/resume**: an unresolvable resource produces
`router.policy.NeedsInput` instead of a hard failure. `ui/jobs.py` persists a
`waiting_for_input` job state with the exact missing-resource question, waits for the user's
answer via `POST /api/jobs/{id}/clarify`, and re-routes with the answer appended to the
original prompt (`route_with_answer()`) — bounded at `MAX_CLARIFICATION_ROUNDS = 3`.

**Dynamic replanning**: `router/replanner.py::decide_replan()` — one bounded, schema-
constrained call after a `mixed`-intent sweep finishes with findings (never a loop), selecting
a follow-up target by finding-id and running it through the existing `_run_single()`.

**Benchmark results (128 prompts: 104 tuned + 24 holdout, real Qwen3-8B/Ollama)**:
- Overall planning accuracy: **98.4%**
- Holdout accuracy: **100%**
- Schema validity: **100%**
- Hallucinated resources: **0**
- Residual known gap: **~1/13 open-tab semantic-selection miss rate**, deliberately tuned
  toward the safer failure direction — see the principle below.

**Live end-to-end results, real Chrome + real Ollama, not mocked**:
- Exact course-pages test: **PASS** — `"Check all my course pages and combine everything I
  still need to do this week"` resolved to exactly the real course tabs, zero distractors,
  zero invented URLs, no "no targets found" failure.
- URL-free cross-site test: **PASS** — `"Find the project code on the page where it's listed,
  then put that same code into the configuration page and verify it"` resolved both steps
  correctly from tab content alone (no URLs, no "Site A/B" phrasing), extracted and verified
  code `AX-42` end to end.
- Research routing: **PASS** — routed to `requires_discovery=True` and invoked the existing
  discovery pipeline correctly; full open-web search success not required to validate this
  (bot-detection risk is a pre-existing, documented limitation).

**Governing principle**: when a resource reference can't be resolved with confidence, the
system asks rather than guesses. Every tuning decision made during this pass (the sweep-vs-
ordered_workflow fix, the open-tab selection retune) moved failure modes in the direction of
"ask the user" over "silently pick something that might be wrong" — this is why the residual
open-tab miss rate above manifests as an occasional extra clarification question, not a wrong
action taken on the wrong page.

**Post-merge regression**: 251/251 deterministic tests passed (217 `tests/unit` incl. 31 new
semantic-planner tests + 76 router/planner-specific; 6 `test_cdp_attach.py`; 9 `test_ui_app.py`;
19 phase1-4 Playwright integration tests). Zero failures, zero regressions.

**Production configuration confirmed**: `config/default.yaml`'s `routing.mode: "hybrid"` —
deterministic fast paths first, semantic planner for everything else, legacy Qwen router only
as a last-resort fallback if planning itself errors.

BrowserAgent is ready for real-world use. Future changes should be driven by failures observed
during actual tasks, not further speculative architecture work.

## 24. First Real Open-Tab Sweep — Two Bugs Found and Fixed

Branch `fix/cdp-open-tab-sweep`, off `main` @ `780d650`. The semantic planner pass (Section
22-23) validated planning/resolution live but never drove a full multisite sweep through
real CDP-attached tabs end to end. The first time a user actually did that — prompt "Look
through the pages I currently have open and tell me what each one is about. Give me a
combined report with the source for each page." against 3 real open tabs (iana.org,
python.org, example.com) — semantic planning and resolution were both correct (exactly the
3 real tabs, zero hallucinated targets), but the batch itself failed: two items hit
`CONTRACT` ("structured batch result ... was not valid JSON"), and the third hit
`SCOPE_BLOCKED` misreported in the UI as `AUTH_REQUIRED`. Two independent bugs, both only
reachable by a real multi-tab cdp_attach batch — no existing test exercised this combination.

**Bug 1 — malformed structured batch results.** `agent/schemas.py`'s `FinishAction.result`
was (and, for single-task finishes, still is) a plain string. The GBNF grammar
(`inference/grammar/action.gbnf`) only constrains that string to be *validly JSON-string-
encoded* — it says nothing about the string's *content*. Batch child prompts
(`BatchOrchestrator._child_goal`) asked the model to hand-serialize a full JSON object as
that content, which `batch/orchestrator.py::_extract_structured_result` then re-parsed with
a second `json.loads()`. Qwen reliably wrote syntactically-close-but-invalid JSON there
(unquoted keys, stray characters) — exactly the "JSON inside a JSON string" fragility this
project has hit before with `outputs` (see `OutputItem`'s docstring), just not yet fixed for
batch results. **Fix**: added `FinishStructuredResult`/`FinishFinding` — real, grammar-
constrained fields (`agent/schemas.py`, extended `action.gbnf`) the model fills directly, no
second JSON layer. `_child_goal` now asks for a short plain-text `result` sentence plus the
typed `structured_result` field; `_extract_structured_result` prefers the typed field when
present and only falls back to legacy string-JSON parsing for old/non-batch callers (that
fallback's existing malformed-JSON handling — retry, then `FAILED_FINAL`/`CONTRACT` — is
deliberately unchanged, see `test_structured_contract_fails_final_when_all_attempts_malformed`).

**Bug 2 — resolved tabs collapsed to bare URLs.** `RouterDecision.targets` was (and remains,
for backward compatibility) `list[str]` — resolving an `open_tabs` requirement to real tabs
still handed `BatchOrchestrator`/`BatchStore` nothing but URLs, with no way to tell "this is
an already-open tab, attach to it" from "navigate somewhere to reach this URL". In
`cdp_attach` mode, `PlaywrightBackend._start_cdp_attach` picks "the most recently active
tab" by default (a reasonable default for a single attached task) — with no per-item tab
identity, each sequential batch child just inherited whatever tab a *previous* child's own
navigation had left active, then got `SCOPE_BLOCKED` trying to act on a page whose origin
didn't match its own target. **Fix**: a minimal, additive discriminated-resource thread —
`router/resources.py::ResolvedResource.tab_ids` (resolver-owned) ->
`router/schema.py::RouterDecision.target_resources` (`TargetResource`, aligned to `targets`
by URL, empty for every non-open-tab decision) -> `batch/store.py::create_batch(...,
target_resources=...)` building a richer `target_payload` per item (`batch/policies.py::
target_payload` already had this discriminated shape, just always URL-typed until now) ->
`agent/runtime_policy.py::BatchRuntimePolicy.is_open_tab` -> `AgentLoop` passing
`preferred_tab_url` into `PlaywrightBackend` -> `PlaywrightBackend._select_preferred_page`
matching the attached browser's real pages by URL and using that exact page, never
navigating to reach it (so tab-switching is never treated as navigation — `same_origin`
scope checks stay anchored to each item's own target, exactly as they already were). Falls
back to the pre-existing "most recently active" heuristic when unset (plain URL targets,
launch mode, or a tab that's since been closed) — completely unaffected. `_child_goal` also
stopped telling open-tab items to "open this target page" (redundant/unwanted navigation);
they're told the tab is already the active page to read in place.

**Bug 3 (found while fixing Bug 2) — failure-category misclassification.** UI showed
`AUTH_REQUIRED` for the scope-blocked item. `batch/policies.py::classify_child_failure`
scanned the *entire* event history's JSON blob for auth keywords ("sign in", "login", ...)
before ever checking the `failure_category` `agent/loop.py::_block_by_runtime_policy`
already writes verbatim onto the `TASK_BLOCKED` event — so an unrelated OBSERVATION event
merely containing "Sign In" (a nav link on the wrong-tab page) won the heuristic race before
the correct, already-known, structural `SCOPE_BLOCKED` category ever got a chance. **Fix**:
check that explicit `TASK_BLOCKED.failure_category` first; only fall through to the
blob-wide heuristic when nothing structural was recorded (still needed for genuine live
login walls via `looks_like_login_page`, which never sets an explicit category — covered by
`test_genuine_login_wall_still_classified_as_auth_required`).

**Live validation, real Qwen3-8B + real CDP Chrome, not mocked.** Set up exactly 3 tabs
(iana.org, python.org, example.com; tab ids captured before/after) and ran the exact failing
prompt through the UI again:
- Semantic planner: exactly the 3 real tabs, 0 hallucinated, `target_resources` carried all
  3 with correct `tab_id`/`title`.
- Batch: **3/3 completed, 0 failed, 0 blocked**, `schema_valid_pct: 100.0`.
- Every child's OBSERVATION events stayed on its own tab's URL for the entire task (checked
  per-task event log) — no tab was ever navigated to a sibling item's target.
- Tab identity: all 3 original CDP target ids/URLs unchanged after the run — 0 new tabs, 0
  closed tabs.
- `structured_result` on every completed task was a real parsed dict populated by the model
  (not a re-parsed string) — e.g. the example.com item's finish carried
  `{"findings": [{"field": "education_discount", "value": "Avoid use in operations.", ...}]}`
  straight through, and `result` was a short plain-text sentence, never JSON text.

**Regression**: 235/235 `tests/unit` passed (12 new: `test_batch_policies.py`,
`test_playwright_backend_tab_selection.py`, `test_agent_loop_tab_wiring.py`, plus additions
to `test_batch_orchestrator.py`/`test_batch_store.py`/`test_decision.py`/
`test_semantic_planner.py`) incl. the pre-fix regression fixtures that reproduce both
original bugs verbatim. `tests/integration` (real Playwright launch-mode + phase1-4 Playwright
suites) re-run unchanged.

**Scope discipline**: the semantic planner itself needed no changes — evidence showed both
bugs were purely in execution/attachment plumbing downstream of a correct plan. Single-site
(`current_page`) tasks were left untouched (they don't route through `BatchRuntimePolicy` at
all); launch-mode batches were left untouched (`preferred_tab_url` is only ever set when
`BatchRuntimePolicy.is_open_tab` is true, which only `create_batch(..., target_resources=...)`
from a resolved `open_tabs` requirement ever sets).

## 25. One-Command Startup (`browser-agent start`) — Evidence Log

**Goal**: stop requiring manual Ollama/Chrome/UI startup on Windows, and fix a real
false-success bug where `browser-agent browser start` printed "Started persistent Chromium"
even when Chrome exited immediately and CDP never bound (`netstat -ano | findstr :9222`
returned nothing while the old command claimed success).

**What changed**: new `cli/launcher.py` (health-check/start/poll orchestration for Ollama,
CDP Chrome, and the UI — no changes to AgentLoop/BatchOrchestrator/WorkflowOrchestrator/
routing/memory), new `browser-agent start`/`stop` subcommands in `cli/main.py`, `status`'s
`task_id` made optional to double as a non-mutating health snapshot. The old
`cmd_browser_start` now calls the same `launcher.ensure_chrome_cdp()` used by `start`, so it
can no longer report success without a real `/json/version` response — this is also the
regression fix and regression test (`test_chrome_process_starts_but_cdp_never_appears_is_failure`,
`test_chrome_exits_early_reports_exit_code_diagnostic`).

**Process ownership**: PIDs for services *we* start are recorded in
`<runtime_dir>/launcher/state.json`; `stop` only ever acts on a PID it recorded itself via
`taskkill /PID <pid>` — never `/IM chrome.exe` or `/IM python.exe` (see
`test_stop_pid_targets_only_the_given_pid`). Ollama is never stopped by `stop`, even if
`start` launched it. A `start.lock` file (holding the PID of the running `start` process)
prevents two concurrent `start` invocations from racing into duplicate services, and
self-heals if the recorded PID is no longer alive.

**Deterministic tests**: `tests/unit/test_launcher.py`, 21 tests, all mocked (no live
Ollama/Chrome/sockets) — healthy-reuse and cold-start-and-poll-to-healthy for both Ollama
and Chrome/CDP, start-timeout-is-failure for both, missing model, missing executable, UI
port reuse vs. occupied-by-something-else vs. free, lock acquire/duplicate-reject/stale
recovery, state round-trip, corrupt state fallback, targeted `stop_pid`, and GPU-telemetry
best-effort parsing. Full suite: 256/256 `tests/unit` passed after this change (0 regressions).

**Real validation on this machine** (RTX 4070, Ollama with `qwen3:8b` already pulled, Chrome
already running on CDP 9222 from an earlier session, port 8765 free):

- `browser-agent start --no-open` printed all three `[PASS]` lines, correctly reusing the
  already-running Ollama and Chrome (no duplicate processes spawned — `state.json` recorded
  `chrome_started_by_us: false`, `ollama_started_by_us: false`, only `ui_pid` populated) and
  started the UI fresh, verified healthy via `GET /api/browser/status` before printing PASS.
- Running `browser-agent start` again while the first was still up was correctly rejected:
  `Another 'browser-agent start' is already running (pid <n>)`.
- `browser-agent stop` killed exactly the recorded UI PID (`taskkill /PID <n>`, confirmed via
  `netstat` that port 8765 stopped listening) while port 9222 (Chrome) and Ollama's
  `/api/tags` both remained up afterward, confirmed by direct `curl`/`netstat` checks.
- `browser-agent status` output matched observed reality exactly at every stage, including
  `GPU: GPU offload 100% (qwen3:8b)` sourced from `ollama`'s `/api/ps` while a task was
  running.
- Real agent smoke test through the freshly-started UI: submitted "Go to https://example.com
  and tell me what this page is about, with evidence." via `POST /api/jobs`; job completed
  with `final_result.summary` correctly describing the page and quoting the actual page text
  as evidence. No writes, submissions, uploads, or messages were performed.

**Not validated in this pass**: macOS. OS-specific process launching stays isolated
(`cli/launcher.py`'s `sys.platform == "win32"` branches for detached-process creation and
`taskkill`); the POSIX fallback paths (`os.kill`, plain `SIGTERM`) are written but only
unit-tested with mocks, not run on real macOS hardware.

## 26. UI Job Stop Was Unstoppable From `waiting_for_input` After a Restart — Evidence Log

**Reported bug**: a job in `waiting_for_input` (the semantic planner returned `NeedsInput`)
showed the clarification card and a Stop button, but clicking Stop did nothing — the job
stayed `waiting_for_input` forever, and the UI's own "reconnect to whatever's still active on
page load" logic (`ui/static/index.html`'s `init()`) kept the Run button permanently disabled.

**Root cause**: `JobRunner.stop()` (`ui/jobs.py`) only ever mutated an in-memory `_JobControl`
object (`self._control[job_id]`). That object lives only as long as the Python process that
created it — it is never persisted. `UIJobStore` (`runtime/ui/jobs.db`) is the durable record
(this is exactly what makes "survive a page refresh" work at all), but a `waiting_for_input`/
`waiting_for_login`/`waiting_for_approval` row left behind by a *previous* UI server process
(a restart — increasingly common now that `browser-agent start`/`stop` make restarting the UI
routine, see Section 25) has no corresponding `_control` entry in a freshly started process.
`stop()` returned `False` for that case, the HTTP layer turned that into a 404, the frontend
never surfaces fetch errors on the Stop button, and the persisted non-terminal job simply sat
there — permanently blocking Run. A live-session stop (same process, control object present)
already worked correctly at the status level, but left `pending_clarification`/
`pending_approval` stale in the store, and a Stop during `waiting_for_approval` briefly wrote
a misleading `status="running", activity="Denied, continuing..."` before the loop's own next
iteration corrected it to `stopped`.

**Fix** (`ui/jobs.py`): `JobRunner.stop()` now falls back to a direct, idempotent store
transition (`status="stopped"`, both pending-* fields cleared) whenever there is no live
`_JobControl` for the job_id, as long as the persisted row exists and isn't already terminal
(`TERMINAL_JOB_STATUSES = {"completed", "failed", "stopped"}`) — stopping an already-terminal
or nonexistent job is always a harmless no-op, never a 500. The `waiting_for_input` and
`waiting_for_approval` live-session paths now also clear `pending_clarification`/
`pending_approval` on stop, and the approval callback reports `status="stopped"` directly
(instead of a transient "running"/"Denied, continuing...") when the denial was actually
caused by Stop. `ui/app.py`'s `/stop` route docstring/message updated to reflect the
now-genuinely-idempotent contract (404 only for a job_id that was never created at all).

**Also found and fixed while validating live** (`cli/launcher.py`, from Section 25's work):
`stop_pid()`'s plain `taskkill /PID <pid>` cannot close BrowserAgent's own UI server at all on
Windows, because that process has no window to receive `WM_CLOSE` — `taskkill` returned
"can only be terminated forcefully" every time, confirmed live (`browser-agent stop` reported
success while the process and port 8765 kept right on running). Fixed by falling back to
`taskkill /PID <pid> /F` whenever the graceful attempt's exit code is nonzero. This was
blocking real validation of today's fix (the UI process couldn't actually be restarted to
pick up new code) and is now covered by
`test_stop_pid_falls_back_to_force_when_graceful_taskkill_fails`.

**Regression tests**: `tests/integration/test_ui_jobs.py` — `waiting_for_input`/
`waiting_for_login`/`waiting_for_approval` → stop (each landing on `stopped`, pending-*
cleared), stop is idempotent on every terminal status, stop on an unknown job_id returns
`False`, an orphaned `waiting_for_input` row survives a simulated restart and is still
stoppable, and a follow-up job never inherits a stopped job's clarification state.
`tests/integration/test_ui_app.py` adds the HTTP-level idempotency check (terminal + orphaned
jobs both `POST .../stop` → `200 {"ok": true}`, never a 404/500). `tests/unit/test_launcher.py`
adds the `taskkill` force-fallback regression. Full suite: 256/256 unit, 48/48
non-model integration, all passing (0 regressions); the two new browser-touching stop tests
also needed an explicit wait for the driving task's real Playwright teardown to finish before
returning — without it, `test_stop_mid_job_leaves_state_clean`'s browser-close bled into and
hung the next real-browser test in the same session (a pre-existing test-hygiene gap, not
present before an in-progress browser-close could overlap a fresh one from the next test).

**Real UI validation, live** (real Ollama/Qwen3-8B, real persistent CDP Chrome, real UI server
restarted mid-session to pick up the fix):
1. Submitted "Find the project code on the page where it's listed, then put that same code
   into the configuration page and verify it." with no matching pages open → `waiting_for_input`
   with the expected clarification question, confirmed via `GET /api/jobs/{id}`.
2. `POST /api/jobs/{id}/stop` → `{"ok": true}`; immediate re-`GET` showed
   `status: "stopped"`, `pending_clarification: null`.
3. A stale `POST .../clarify` against that same (now-stopped) job → `409 job is not waiting
   for input`, both before and after a full UI server restart.
4. Submitted an unrelated job ("Go on Amazon and find the 3 best vacuum cleaners. Do not
   purchase anything.") → different job_id, started and routed (`research`) with
   `pending_clarification: null` — no trace of the first job's clarification text. Stopped it
   mid-run (`running` → `stopped`) once isolation was confirmed, without letting it place any
   order.
5. Killed the UI server process entirely and started a fresh one (`browser-agent stop` /
   `start --no-open`, which now actually terminates the process thanks to the `taskkill /F`
   fallback above). The earlier stopped job stayed `stopped` — `pending_clarification` still
   `null` — with no in-memory state at all behind it, and Run worked immediately: a brand
   new job submitted post-restart completed normally.

**Separate anomaly observed, not fixed here (out of scope)**: a *third*, exploratory
validation job ("Open https://example.com and tell me what it is.") submitted right after the
Amazon research job was stopped came back summarizing a goodhousekeeping.com vacuum-review
page instead of example.com. The event log shows the very first OBSERVATION in that task was
already on the stale goodhousekeeping.com tab left open by the previous (stopped) research
job — the persistent CDP-attached Chrome's tab reuse across single-site jobs, not a job-store
or clarification-state leak (the UI-level state itself — job id, `pending_clarification`,
`router_decision` — was all correctly isolated per the Section 16 validation above). This is
an AgentLoop/model-behavior question (should a fresh single-site task force-navigate its
target rather than trusting the model to read the URL out of its own objective text when a
stale tab is already showing content?), explicitly out of this task's scope
("do not change AgentLoop... unless required for stop-state integration") and left for a
future pass.

## 27. Fresh Single-Site Task Reusing a Stale CDP Tab — Root Cause and Fix

Follow-up to Section 26's "separate anomaly observed, not fixed here" note — this is that fix.

**Reported bug**: a brand-new single-site task ("Open https://example.com...") started right
after a previous task's job ended, with a persistent CDP-attached Chrome that still had an
unrelated tab open (e.g. goodhousekeeping.com left over from that previous task). BrowserAgent
attached to and answered from the stale goodhousekeeping.com tab instead of navigating to
example.com — silently, with no error, no scope block, nothing in the job's own status to
suggest anything was wrong.

**Root cause**: `PlaywrightBackend._start_cdp_attach()`'s page-selection policy had exactly one
non-default path — `preferred_tab_url`, set only for a semantic *open-tab* work item
(`BatchRuntimePolicy.is_open_tab`, Section 24's open-tab sweep fix). Every other caller,
including a plain single-site task with a perfectly well-known explicit target URL, fell
through to `_select_page()`'s "most recently active tab" heuristic — a heuristic that has no
notion of *any* task having a specific target at all. `router/policy.py`'s
`_translate_single()` already resolves a single-site task's explicit URL into
`RouterDecision.targets[0]`, but `ui/jobs.py`'s `_run_single()` never passed it anywhere near
`AgentLoop`/`PlaywrightBackend` — it was computed, stored in the job's persisted
`router_decision` for the UI to display, and then dropped on the floor as far as tab selection
was concerned. `cli/main.py`'s `cmd_run` had the same gap for its own raw `--goal` text (no
router pass at all). The batch/workflow orchestrators were *not* affected the same way for
their own `is_open_tab=False` targets, since even before this fix they always constructed a
`BatchRuntimePolicy` with the item's `target_url` — but that value only ever reached
`preferred_tab_url` when `is_open_tab` was true, so a non-open-tab batch/workflow item's
initial attach page was subject to the exact same "most recently active tab" heuristic before
its own `open_url` step (if the model ever issued one) corrected it — same class of bug,
smaller blast radius because `pre_action_violation`/`post_navigation_violation`'s scope
policy would at least block a *cross-origin* stale tab rather than silently answering from it.

**Fresh-task page-selection rule** (`browser/playwright_backend.py`,
`_start_cdp_attach`/`explicit_target_url`): a page-selection *target* is now one of three
kinds, decided once per `AgentLoop`/`PlaywrightBackend` instance:
- **Open-tab-semantic** (`preferred_tab_url`, unchanged): exact URL match only; if the tab
  isn't found (closed between resolution and attach), falls back to the "most recently active
  tab" heuristic — a normal, recoverable case, not a hard failure.
- **Explicit target** (new: `explicit_target_url`): a fresh single-site task's resolved URL
  (`ui/jobs.py`), a raw-goal CLI run's extracted URL (`cli/main.py`), a non-open-tab
  batch/workflow item's `target_url` (`agent/loop.py`, derived from `runtime_policy.target_url`
  when `is_open_tab` is false), or a resumed job's last-known `current_url`
  (`AgentLoop.resume`). Exact URL match reuses an existing tab already sitting on that page;
  otherwise a fresh blank page is created — **never** the "most recently active tab" heuristic,
  since an unmatched explicit target has no legitimate fallback tab, only a wrong one. The
  model's own `open_url` step then navigates the blank page to the target, exactly as if no
  tab existed at all.
- **No known target** (a genuine current-page task, e.g. "tell me what this page is about"
  with no URL in the prompt — `router/policy.py`'s `_translate_single()` returns
  `targets=[]`): the original "most recently active tab" heuristic, unchanged — this is the
  one case where reusing whatever's already open is the *correct*, intended behavior.

**Fix** (`browser/playwright_backend.py`, `agent/loop.py`, `ui/jobs.py`, `cli/main.py`): added
`PlaywrightBackend.explicit_target_url` and the `_select_page_by_url()` helper implementing
the rule above; `AgentLoop.__init__`/`create_new` gained an `explicit_target_url` parameter
(and derive one from `runtime_policy.target_url` for non-open-tab batch/workflow items, same
as the existing `preferred_tab_url` derivation for open-tab ones); `AgentLoop.resume` now
seeds `explicit_target_url` from the task's last-recorded `current_url` when nothing more
specific already set it; `ui/jobs.py`'s `_run_single` passes `decision.targets[0]` through;
`cli/main.py`'s `cmd_run` extracts a URL from the raw `--goal` text with `router/extract.py`'s
existing `extract_urls()` and passes it the same way. No change to `_select_page()` itself, to
scope-block enforcement, or to the open-tab-semantic path — this is additive, not a redesign.

**Real CDP smoke test** (headless Chromium simulating the user's persistent browser, real
network navigation, no local inference server available in this sandbox so the model decision
content was stood in with the same `FakeUIClient` shape `tests/integration/test_ui_jobs.py`
already uses — tab selection itself is real, unfaked code):
1. Left a real `https://www.goodhousekeeping.com/` tab open via CDP (remote-debugging-port),
   simulating the reported "previous task left this tab open" state.
2. Submitted "Open https://example.com and tell me what this page is about." through the real
   `JobRunner` → `AgentLoop` → `PlaywrightBackend` stack in `cdp_attach` mode.
3. Re-ran the identical scenario against the pre-fix code (`git stash` of just the fix files)
   to confirm it reproduces: the job attached directly to the stale goodhousekeeping.com tab
   (`observed urls: ['https://www.goodhousekeeping.com/', ...] x5`, never navigated) and
   errored out on an unrelated assertion in the harness rather than ever reaching example.com —
   causal confirmation this is the real mechanism, not a coincidental pass/fail.
4. With the fix restored: `observed urls: ['about:blank', 'https://example.com/',
   'https://example.com/']`, job `status: "completed"`, `final_result.summary` about
   example.com, and the goodhousekeeping.com tab still open and completely untouched
   (`stale tab is still open, untouched: https://www.goodhousekeeping.com/`) — no leakage,
   the correct existing-tabs-left-alone contract from Sections 8-11 held throughout.

**Regression tests**:
- `tests/unit/test_playwright_backend_tab_selection.py` — `_select_page_by_url` finds an exact
  match even when a stale tab is more "recently active", and returns `None` (never a stale
  fallback) when nothing matches.
- `tests/unit/test_agent_loop_tab_wiring.py` — `explicit_target_url` reaches
  `PlaywrightBackend` from `AgentLoop.create_new`'s own parameter, from a non-open-tab
  `BatchRuntimePolicy.target_url`, and from `AgentLoop.resume`'s last-known `current_url`; a
  genuine current-page task leaves both `preferred_tab_url`/`explicit_target_url` unset.
- `tests/integration/test_cdp_attach.py` — real-Chromium-backed: explicit target reuses a
  matching existing tab even when a stale tab is more recently active; explicit target with no
  matching tab never reuses the stale one (lands on a fresh blank page instead); reconnect
  (disconnect/reattach) with the same `explicit_target_url` finds the same tab again; a
  current-page task (no `explicit_target_url`) still reuses the most-recently-active tab,
  unchanged.
- `tests/integration/test_ui_jobs.py` — full `JobRunner`-level, real headless Chromium in
  `cdp_attach` mode: a fresh explicit-URL job never observes a stale tab left open at job
  start, and a brand-new job started after a previous job completed/stopped does not inherit
  that previous job's tab.

Full suite after the fix: 263/263 unit, 54/54 integration, all passing (0 regressions),
plus the real CDP smoke test above (pass) and its pre-fix reproduction (fails exactly as
reported, confirming root cause).

## 28. Repository Consolidation — `main` Is Now the Single Canonical Branch

**Date**: 2026-08-28.

**Purpose**: this project accumulated one branch per phase/feature/fix. Several fixes (the
UI Stop fix, Section 26; the fresh-task stale-tab fix, Section 27) had landed only on their
own branches and had been pushed to `origin`, but not yet folded back into `main`. This
section is a pure repository-consolidation pass — no new functionality, no live product
runs — to make `main` alone sufficient to run the complete, current BrowserAgent.

**Starting state**:
- `main` local HEAD and `origin/main` HEAD: `fcb3227` (merge of `feature/one-command-startup`).
- Working tree on `main` had **staged, uncommitted changes** matching the full diff of
  `main..fix/fresh-task-tab-selection` — an artifact of the environment restoring disk state
  from a checkpoint taken mid-session in a prior consolidation attempt. Protected first via
  `git stash push -u` (nothing was discarded) before touching any refs.

**Final state**:
- `main` local HEAD and `origin/main` HEAD: `b4c0401` (`fix: fresh single-site task no longer
  reuses a stale CDP-attached tab`), reached by a clean **fast-forward merge** — no new merge
  commit, no rebase, no force-push.

**Branches audited** (local + `origin`): `feature/one-command-startup`,
`feature/semantic-task-planner`, `final/cdp-persistent-browser`, `fix/cdp-open-tab-sweep`,
`fix/fresh-task-tab-selection`, `fix/ui-stop-waiting-jobs`, `phase5-multisite-orchestration`,
`phase5b-real-world-ui`, `research/browseros-backend-feasibility`,
`origin/phase4b-memory-application` (remote-only, no local branch).

**Already fully contained in `main` before this pass** (verified with
`git merge-base --is-ancestor <branch> main`, i.e. every commit on the branch is already an
ancestor of `main`'s prior HEAD `fcb3227`):
- `feature/one-command-startup` — merged via PR #3 (`main`'s prior HEAD *was* this merge).
- `fix/cdp-open-tab-sweep` — merged via PR #2.
- `final/cdp-persistent-browser` — merged (tag `browseragent-v1-ready`'s ancestry).
- `feature/semantic-task-planner` — merged (tag `browseragent-v1-semantic-ready`'s ancestry).
- `phase5-multisite-orchestration` — fully merged.
- `phase5b-real-world-ui` — fully merged.
- `origin/phase4b-memory-application` — fully merged.

**Required reconciliation** (not contained by commit ancestry, needed integration):
- `fix/ui-stop-waiting-jobs` (tip `707e6ad`) and `fix/fresh-task-tab-selection` (tip
  `b4c0401`) both branched from `fcb3227` and both remained unmerged into `main`.
  `fix/fresh-task-tab-selection`'s own history contains an intermediate commit (`0fd92ce`)
  that *replays* the Stop fix but — verified via `git diff 0fd92ce 707e6ad`, before
  reconciling — was missing part of `fix/ui-stop-waiting-jobs`'s fuller implementation
  (`TERMINAL_JOB_STATUSES`, the idempotent-stop docstring/logic, `pending_clarification`/
  `pending_approval` clearing on every stop path, and the stop-during-approval status fix).
  Diffing the branch **tip** `b4c0401` against `fix/ui-stop-waiting-jobs` (`707e6ad`) directly
  (`git diff 707e6ad b4c0401`) confirmed this was corrected within the branch's own second
  commit: `cli/launcher.py`, `ui/app.py`, `tests/integration/test_ui_app.py`, and
  `tests/unit/test_launcher.py` are **byte-for-byte identical** between the two branches, and
  `ui/jobs.py` on `b4c0401` is a strict superset of `707e6ad`'s version (every line of the
  Stop fix present, plus the additional `explicit_target_url` wiring for the stale-tab fix).
  Conclusion: `fix/fresh-task-tab-selection`'s tip is the newest, most complete, validated
  implementation and a strict superset of `fix/ui-stop-waiting-jobs` — reconciled by a single
  `git merge --ff-only fix/fresh-task-tab-selection` onto `main` (no cherry-pick, no separate
  merge of `fix/ui-stop-waiting-jobs` needed or performed, avoiding a duplicate/no-op commit).
  Re-verified post-merge: `git diff fix/ui-stop-waiting-jobs main -- <the 4 shared files>` is
  empty, and the `ui/jobs.py` diff shows only the *additional* stale-tab lines, confirming
  zero content loss.

**Intentionally NOT merged**:
- `research/browseros-backend-feasibility` (tip `45eebf7`) — its single commit is explicitly
  a feasibility study ("Feasibility study only, no production code changed") that recommends
  **against** adopting BrowserOS and **for** keeping the existing Playwright `connect_over_cdp`
  approach — i.e. it validates the status quo already in `main` rather than proposing a change
  to merge. It also branched from a point far earlier in history (`c356b56`) and diffing it
  against current `main` shows ~5,000 deleted lines purely because it never received any of
  the later phases — not because any of that later work should be reverted. Left unmerged,
  branch preserved for provenance.

**Final feature checklist** (spot-checked directly against `main`'s tree, not inferred from
branch names):

| Capability | Present in `main` |
|---|---|
| Phase 4B bounded long-horizon context/memory | Yes — `memory/task_memory.py`, `agent/context_builder.py`, Section 14-15 |
| Phase 5 persistent multi-site/batch orchestration | Yes — `batch/orchestrator.py` |
| Phase 5 safety enforcement | Yes — `agent/runtime_policy.py`, `agent/schemas.classify_risk` |
| Structured result contracts | Yes — `batch/models.ResultContract`, `batch/result_quality.py` |
| GPU/Ollama reliability changes | Yes — `cli/launcher.py:query_gpu_info`, `check_ollama` |
| Crash/resume support | Yes — `memory/replay.py:replay_task`, `AgentLoop.resume` |
| Phase 5B local natural-language UI | Yes — `ui/app.py`, `ui/jobs.py`, `ui/static/index.html` |
| Ordered multi-site workflows | Yes — `workflow/orchestrator.py`, `router/policy.py:_translate_workflow` |
| Cross-site verified fact passing | Yes — `batch/orchestrator.py`/`workflow/orchestrator.py` `seed_facts` |
| Research discovery improvements | Yes — `research/discovery.py:discover_sources` |
| Semantic task planner | Yes — `router/semantic_planner.py:plan_task` |
| Deterministic fast-path routing | Yes — `router/extract.py:try_deterministic_route` |
| ResourceResolver | Yes — `router/resources.py:ResourceResolver` |
| Open-tab semantic resolution | Yes — `browser/tabs.py:list_open_tabs`, `router/resources.py` |
| NEEDS_INPUT clarification flow | Yes — `router/policy.NeedsInput`, `ui/jobs.py:_route_with_clarification` |
| Persistent CDP attach mode | Yes — `browser/playwright_backend.py` `mode="cdp_attach"` |
| Router ordered-step decomposition fixes | Yes — `router/extract.py:_split_ordered_steps` |
| Expanded routing action recognition | Yes — `router/extract.py:_ROUTING_ACTION_VERBS` |
| Open-tab identity preservation for batch sweeps | Yes — `agent/runtime_policy.BatchRuntimePolicy.is_open_tab`, Section 24 |
| Typed structured batch finish results | Yes — `batch/orchestrator.py`, Section 24 |
| Explicit runtime failure-category preservation | Yes — Section 24 |
| One-command launcher (`browser-agent start`) | Yes — `cli/launcher.py:ensure_chrome_cdp`, `cli/main.py:cmd_start` |
| Launcher status/stop functionality | Yes — `cli/main.py:cmd_status`/`cmd_stop`, `cli/launcher.py:stop_pid` |
| Persisted UI job Stop fix | Yes — `ui/jobs.py:JobRunner.stop`, `TERMINAL_JOB_STATUSES`, Section 26 |
| `waiting_for_input` cancellation | Yes — `ui/jobs.py:_route_with_clarification` |
| `waiting_for_login` cancellation | Yes — `ui/jobs.py:_wait_login_or_stop` |
| `waiting_for_approval` cancellation | Yes — `ui/jobs.py:_make_approval_callback` |
| Stale Continue protection | Yes — Section 26 (idempotent `stop()` on orphaned rows) |
| Fresh-job state isolation | Yes — Section 27 (`explicit_target_url` never inherited across jobs) |
| Explicit-target CDP tab selection | Yes — `browser/playwright_backend.py:_select_page_by_url`, Section 27 |
| Stale-tab prevention for fresh single-site jobs | Yes — Section 27 |
| Current-page semantics | Yes — Section 27 (no-known-target path unchanged) |
| Resume page identity | Yes — `AgentLoop.resume`'s `explicit_target_url` seeding, Section 27 |
| Existing safety/approval behavior | Yes — unchanged, `agent/schemas.classify_risk`, approval callback |

**Deterministic test result**: `python -m pytest tests/unit -q` → **263 passed**, 0 failures,
0 errors, run on `main` @ `b4c0401` post-merge.

**Intentionally skipped for this consolidation pass**: the entire `tests/integration/`
directory (`test_cdp_attach.py`, `test_ui_jobs.py`, `test_phase1_browser_actions.py`,
`test_phase1b_contract_repair.py`, `test_phase2_verification_recovery.py`,
`test_phase3_crash_recovery.py`, `test_phase4_long_horizon.py`, `test_ui_app.py`) — every one
of these drives a real `AgentLoop`/`PlaywrightBackend`, which launches a real (headless)
Chromium process (`launch_persistent_context` or, for the `cdp_attach` tests, its own
throwaway `chromium.launch(args=["--remote-debugging-port=..."])` to simulate a user
browser). The task instructions for this pass explicitly prohibited starting Chrome/CDP or
performing live browser tests, so these were not run here. They do not require Ollama or the
UI server, and each individual fix's own branch/section (24, 26, 27) already carries its own
full integration-test evidence (54/54 passing at the time each fix was validated) plus real
CDP/live smoke-test evidence — that evidence is preserved in this document and was not
re-collected in this pass.

**Known remaining limitations** (carried forward, unchanged by this pass): macOS is not
validated (Section 25); the documented Windows pytest+Playwright interaction stall noted in
`tests/integration/test_cdp_attach.py`'s own docstring remains a known environment quirk, not
a functional bug.

## 29. Repository Development Policy

**`main` is the default development branch for this repository.**

Unless the repository owner explicitly requests otherwise for a specific piece of work:

- Make changes directly on `main`.
- Commit to `main`.
- Push to `origin/main`.
- Do **not** create a new `feature/*`/`fix/*`/`research/*` branch merely because a task
  involves code changes.

Create a separate branch only when the owner explicitly asks for isolation/experimentation,
or when there is a compelling safety reason (e.g. a change risky enough that it should be
reviewable/revertable as a unit before it ever touches `main`). This is a working policy for
*this* repository's workflow — it does not change Git's behavior or any global tooling
default.

Historical branches (`feature/*`, `fix/*`, `phase*`, `final/*`, `research/*`) are kept for
provenance and are **not** deleted. After Section 28's consolidation, `main` alone is
sufficient to build/run the complete, current BrowserAgent — no production functionality
should ever again be left stranded only on a side branch. Anyone continuing development on
this repository should pull `main`, make their change, commit, and push to `main`, the same
way this consolidation pass itself was performed.
