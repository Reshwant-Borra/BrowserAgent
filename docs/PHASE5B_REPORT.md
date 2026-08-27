# Phase 5B Report: Natural-Language Router, Local UI, Ordered Multi-Site Workflows

Date: 2026-08-26

Branch: `phase5b-real-world-ui`

Starting baseline: `5dcca8e docs: update phase5 gpu scaling evidence` (`phase5-multisite-orchestration`)

## Verdict

**PARTIAL.**

The full new architecture — natural-language task router, local web console, ordered
multi-site action workflows, manual-login handoff, and an HTTP-native approval flow — is
built, wired directly into the existing `AgentLoop`/`BatchOrchestrator` engine (no second
implementation), and covered by 193 passing deterministic tests plus a real, reduced live-
Qwen3-8B validation slice (not the full 50-prompt / 10+10-trial / 20-30-site gates the
original spec asked for — see "Scope decision" below).

Everything the spec's PASS bar requires is either demonstrated with real evidence or clearly
labeled as deferred. The one capability that is not yet reliable enough to call solid is
**cross-site structured fact-passing** (Section 33/34): Qwen3-8B does not consistently
populate the `facts` field a later workflow step depends on, even with an explicit
instruction — this is a genuine, reported model-reliability gap, not an infrastructure bug,
and it's why the verdict is PARTIAL rather than PASS (Section 69's own criterion: "ordered
workflows... remain systematically unreliable" on one specific capability, not everything).

## Scope decision (made with the user before implementation)

The full spec asks for a 50-prompt router benchmark, 10+10 ordered-workflow trials, a
20-30-site public pilot, and a real authenticated-LMS walkthrough. Each real trial is a live
browser+LLM run; the full set is realistically hours of wall-clock time, and the LMS test
needs the user physically present to log in. The user chose: build the complete architecture
this pass, validate with a **reduced but real** live slice, and defer the large-scale gates
to follow-up runs (exact commands given below) rather than simulate or estimate them.

## What was built

- `router/` — deterministic URL-extraction + rule-based routing (`extract.py`), a Qwen3-8B
  structured-output fallback for genuinely ambiguous prompts (`llm_router.py`), and a single
  `route()` entrypoint (`policy.py`) that never lets the model invent a target beyond what the
  user's text already contains.
- `ui/` — a FastAPI app (`app.py`) + one static HTML/JS console (`static/index.html`, no
  build step, no frontend framework) + a `JobRunner` (`jobs.py`) that drives `AgentLoop.step()`
  / `BatchOrchestrator.run()` / `WorkflowOrchestrator.run()` directly so it can publish
  progress, bridge approval/login pauses to HTTP requests, and honor a cooperative stop flag.
- `workflow/` — ordered multi-site action workflows: `WorkflowOrchestrator` runs each step
  through the existing `AgentLoop` engine (same `AgentLoopChildRunner` reuse pattern as
  `BatchOrchestrator`), verifies before advancing, retries verification failures, blocks (does
  not silently continue) once retries are exhausted, and passes structured facts discovered by
  one step into the next step's goal as an explicit block.
- `agent/loop.py` — two small, additive changes: an optional `approval_callback` (async,
  replaces the CLI's blocking `input()` when set) and a live login-wall detector
  (`agent/auth_detect.py`) that short-circuits straight to a `login_required` block instead of
  burning the full recovery ladder.
- `cli/main.py` — `browser-agent ui [--host] [--port] [--no-browser]`.

## User experience

```powershell
browser-agent ui
```

Opens `http://127.0.0.1:8765` (configurable). Type a plain-English task, press Run. See
`docs/USING_BROWSERAGENT.md` for the full walkthrough.

Manually driven end-to-end via real HTTP requests against a live `browser-agent ui` server
(the same API surface the static page's JS calls) rather than a literal rendered-browser
click-through — this session's sandbox had no connected `claude-in-chrome` browser extension
to drive the actual page. Confirmed this way, against the live server, with real Qwen3-8B:
single-site prompt → routed and completed; multi-URL prompt → routed as a sweep and
completed (3/3); research prompt → routed correctly (see "Research mode" below for why it
didn't complete); task history persisted and listed correctly across requests. The static
page itself (`ui/static/index.html`) was not visually inspected in a real browser this pass —
worth a quick manual open before relying on it for daily use.

## Natural-language router

**Deterministic slice** (`tests/unit/test_router.py`, no model — this is the "N URLs is
obvious" bucket Section 10 says should never touch Qwen): 39 tuned+holdout prompts across
single-site read, single-site action, multisite sweep, ordered workflow, research, and
deliberately-ambiguous — **100% correct** (the ambiguous prompts are scored as correct when
they correctly defer to the model rather than guess).

**Live-model fallback slice** (`benchmarks/run_phase5b_router_live.py`, real Qwen3-8B/Ollama,
15 maximally-ambiguous prompts — the only bucket the fallback path is ever reached for by
design): **15/15 schema-valid, 15/15 no hallucinated targets.** Several were classified with
an empty target list (internally consistent, not executable as-is) — see "Remaining failure
modes" below; there's no ground truth for what a prompt with zero actual signal "should"
route to.

This is 54 prompts total (39 deterministic + 15 live), short of the spec's full 50-tuned +
holdout-unseen gate structure but covering every routing bucket with real evidence in both
the deterministic and live-model paths. Exact command to extend to the full 50+holdout gate
is in "Next phase" below.

## Multi-site action workflows

Real Qwen3-8B/Ollama, `benchmarks/run_phase5b_workflow_trials.py`, 3 scenarios (1 tuned
3-site reversible-action, 1 cross-site-dependency, 1 holdout with different labels/layout/
values/order) — run twice, before and after a targeted prompt fix (see below):

| Run | Full-workflow success | Steps verified |
|---|---|---|
| Before fix | 1/3 (33%) | 5/8 |
| After fix  | 2/3 (67%) | 7/8 |

This is far short of the spec's 90-95% gate — expected, given only 3 reduced-slice trials,
not the full 10+10. Both runs surfaced real, fixable issues:

- **Trial 1 (before fix):** the `cross_site_dependency` scenario's step 1 clicked through to
  step 2's page instead of extracting the fact on its own page first (a "Next" link on the
  fixture tempted premature navigation). The `holdout` scenario's step 3 produced a `finish`
  JSON with a literal unescaped quote inside a string value, breaking JSON parsing.
- **Fix applied** (`workflow/orchestrator.py::_step_goal`): added an explicit "stay on this
  page; don't navigate elsewhere" instruction, and an explicit "no literal quotes inside
  string values" instruction.
- **Trial 2 (after fix):** the navigation and malformed-JSON issues did not recur in either
  re-run they'd previously triggered on. The `tuned` and `holdout` scenarios both went 3/3
  steps verified. The `cross_site_dependency` scenario still failed — see next section.

## Cross-site dependency

The one specifically-required cross-site-dependency scenario (Section 33: "find the project
code on site A, enter it on site B") ran twice. Step 1 (find the code) completed and verified
correctly both times — but its `finish` JSON's `facts` field came back **empty** (`{}`) both
times despite an explicit instruction to populate it, so step 2 never received the code,
searched for it itself, and ran out its step budget (`MAX_STEPS`). This is a genuine
Qwen3-8B reliability gap in structured-field population competing against a longer free-form
`summary`/`evidence` response, not an orchestration bug — the workflow-fact-passing
*mechanism* (`WorkflowStore.facts_so_far`, explicit "Known facts" block injected into the
next step's goal) is exercised correctly per `tests/unit/test_workflow_orchestrator.py`'s
`test_workflow_passes_structured_facts_between_steps`; the model just isn't reliably filling
its half of the contract yet. See "Remaining failure modes" for the candidate fix.

## Assignment sweep through the router (no manual batch CLI)

Real Qwen3-8B/Ollama, `benchmarks/run_phase5b_assignment_sweep_live.py`, against the existing
Phase 5 multisite assignment-fixture generator (6 pages, 4 ground-truth actionable
assignments) — the prompt was `"Check these pages and tell me every assignment I still have
to do:\n<6 pasted URLs>"`, submitted through `router.policy.route()` exactly as a user would
paste into the UI (never touching `browser-agent batch run`):

- Routed correctly as `multisite_sweep` with the `assignment` result contract, targets
  preserved exactly as pasted.
- **6/6 pages completed, 0 failed.**
- **Precision 1.00, recall 1.00** against ground truth (4/4 true positives, 0 false
  positives) — matches Phase 5's own assignment-extraction evidence, now reached through the
  natural-language router instead of a manually-constructed batch command.

## Research mode

Two real end-to-end attempts through the live UI server (`browser-agent ui`), prompt:
`"Research the benefits of the Pomodoro technique for studying and give me an evidence-backed
summary."` Routed correctly (`task_type=research`, `requires_discovery=true`, no
hallucinated targets). Both attempts **failed at the source-discovery step**:

- **Attempt 1:** the discovery `AgentLoop` correctly navigated to a DuckDuckGo results page
  and found real, relevant URLs — its `finish` JSON (with up to 20 URLs requested) got
  truncated by the output-token budget mid-string, failed to parse, and the resulting
  fallback action looped for the rest of the step budget instead of finishing. Fixed by
  capping the discovery listing at `min(max_sources, 10)` and explicitly asking for a short
  list (`ui/jobs.py::_discover_sources`).
- **Attempt 2 (after fix):** no more truncation, but the model got stuck repeatedly calling
  `extract` on the same single search-result element instead of enumerating multiple results
  and finishing — never produced a `finish` decision within the 8-step search budget.

**Research/discovery is not yet reliable** — this is real, honestly-reported evidence, not
papered over. The pipeline design (bounded search → cap/dedupe → hand off to the existing,
already-proven `BatchOrchestrator`) is sound and unit-testable; the specific weak point is a
single `AgentLoop` task's ability to enumerate multiple links from one search-results page
using only single-target actions. See "Remaining failure modes" for the candidate fix.

## Manual login handoff

Covered by `tests/integration/test_ui_jobs.py::test_manual_login_wait_and_continue`
(deterministic, scripted model) end to end: task hits a simulated login wall
(`tests/fixtures/simple_site/workflow_login_required.html`, has a real `type="password"`
field so it triggers the same detector a real site would), UI surfaces "Login required,"
`login_continue()` resumes the **same live browser tab/session** (no reconnect, no new
profile) once the page no longer looks like a login page, and the task completes normally.

A real, authenticated walkthrough against the user's own school LMS was **not** run this
pass — it needs the user physically present to type credentials, which is out of scope for
an unattended session. It's documented as a self-serve next step below.

## Approval flow

Covered by `tests/integration/test_ui_jobs.py::test_approval_flow_approve` and
`test_approval_flow_deny_blocks_task`: a consequential action (`click` on a button literally
labeled "Submit Application," `agent/schemas.py`'s existing `classify_risk` keyword
classifier) pauses the task, the UI shows what's pending, `approve(job_id, True)` lets the
action proceed and the task completes; `approve(job_id, False)` blocks the task with a
recorded reason instead of proceeding. The approval callback threads through
`AgentLoop`/`BatchOrchestrator`'s `AgentLoopChildRunner`/`WorkflowOrchestrator` uniformly.

## Stop / resume

Covered by `tests/integration/test_ui_jobs.py::test_stop_mid_job_leaves_state_clean`:
stopping mid-job lets the in-flight step finish (never corrupts state), then halts before the
next step; already-completed steps/items remain in the batch/workflow store. Page-refresh
recovery: `GET /api/jobs/{id}` always re-derives status from `UIJobStore` (persisted sqlite),
independent of any open HTTP/SSE connection — verified via the same job-store round-trip the
deterministic UI-API tests exercise.

## Phase 5 regression

Every Phase 5 unit/integration test still passes unmodified — Phase 5B added new tests and
two small, additive changes to `agent/loop.py` (optional `approval_callback`, live login
detection) and `batch/orchestrator.py`/`batch/policies.py` (optional `approval_callback`
parameter threaded through `AgentLoopChildRunner`, shared `agent/auth_detect.py` keyword
list), none of which change existing call sites' behavior when the new parameters are left at
their defaults. `tests/unit/test_batch_orchestrator.py` (Phase 5's own batch orchestrator
suite) passes unchanged.

## Tests

193 deterministic tests total (132 pre-existing + 61 new this phase: 39 router, 6 workflow
orchestrator, 9 UI HTTP-API, 7 UI JobRunner). Verified passing in reliable batches this pass:

- `pytest tests/unit tests/model -m "not model"` — **160 passed** (11.4s)
- `pytest tests/integration --ignore=test_ui_app.py --ignore=test_ui_jobs.py` (pre-existing
  Phase 1-4) — **17 passed** (28-30s)
- `pytest tests/integration/test_ui_app.py` — **9 passed** (3.6s)
- `pytest tests/integration/test_ui_jobs.py` — **7 passed**, confirmed multiple times in
  isolation (2-8s) and paired with `test_ui_app.py` (13.4s together)

160 + 17 + 9 + 7 = 193, matching the full-suite count. The complete `pytest tests/ -m "not
model"` invocation in one process passed cleanly twice earlier in this session (**193
passed, 2 deselected** in 46.9s at one point) but became intermittently unreliable late in
the session, after dozens of cumulative live-model benchmark runs and manual UI-server
sessions had each launched their own Playwright browser processes — a single combined pytest
invocation would occasionally stall on a browser launch partway through. Isolating the
underlying `PlaywrightBackend.start()`/`launch_persistent_context()` call and running it
directly (outside pytest) always completed in under 1 second, even while the stall was
reproducing inside pytest, which points at Windows/pytest-asyncio-specific resource
accumulation across a very long single-process test run rather than a defect in the browser
backend or the new code itself. Recommended mitigation if this persists in CI: run
`tests/unit`, `tests/model`, and `tests/integration` as separate `pytest` invocations (as
verified above) rather than one combined command.

## Safety

Zero unintended consequential writes across every trial run this pass (live workflow trials,
live assignment sweep, live research attempts, deterministic approval tests). The approval
gate is unconditional: `agent/schemas.py::classify_risk` (unchanged from Phase 1B) still
flags any action whose target element name matches a consequential keyword regardless of the
router's inferred policy, and blocks or asks — the router's `preferred_policy` only ever
controls the *default* `read_only`/reversible-actions runtime policy, never a bypass of the
approval gate itself. `test_approval_flow_deny_blocks_task` confirms a denied consequential
action blocks the task rather than being silently retried or skipped.

## Real-world robustness fixes

- `agent/auth_detect.py` — new live login-wall detector (password-type input, or a login
  keyword in the title/first-heading text), reused by both the live in-loop check
  (`agent/loop.py::step`) and the existing post-hoc batch-failure classifier
  (`batch/policies.py::classify_child_failure`, previously a private, duplicated keyword
  check).
- `workflow/orchestrator.py::_step_goal` — added "stay on this page" and "no literal quotes
  in string values" instructions after live trials surfaced both failure classes (see
  "Multi-site action workflows" above).
- `ui/jobs.py::_discover_sources` — capped the research-discovery URL listing to avoid
  output-token truncation (see "Research mode" above).
- `tests/conftest.py::tmp_config` — fixed a test-isolation bug found during this pass: the
  shared fixture never overrode `StorageConfig.runtime_dir`, so any test constructing a
  `ui.app.create_app`/`ui.jobs.JobRunner` against it was silently reading/writing the **real**
  `./runtime/ui/jobs.db` instead of an isolated temp directory — confirmed by a real UI job's
  result getting wiped by `test_history_lists_and_clears`'s clear-history call during this
  session. Fixed by setting `runtime_dir=str(tmp_path / "runtime")` in the fixture.

## Model decision

**QWEN3-8B SUFFICIENT** for the routing, single-site, and multi-site-sweep capabilities
(100% deterministic router accuracy, 15/15 live-fallback schema validity, 1.00/1.00
precision/recall on the live assignment sweep). **Not yet sufficient, on its own, for
reliable structured multi-field JSON population under a longer free-form response** (the
cross-site `facts` field and the research-discovery URL-enumeration failures are both this
same underlying pattern) — the candidate fix is architectural (split structured-field
extraction into its own smaller, more constrained call) rather than a model swap; no
evidence collected this pass points at model capacity being the limiting factor over prompt/
task decomposition.

## Vision decision

**VISION NOT YET JUSTIFIED.** No trial in this pass — deterministic, live-workflow, or
manual — hit a page whose control could not be represented in the existing compact
accessibility/interactive-element extraction (`browser/page_model.py`). No evidence collected
that would justify adding screenshot-based observation even as a fallback.

## Domain skills decision

**NOT YET JUSTIFIED.** The same real site was not driven repeatedly enough in this pass (3-6
live trials per scenario, not tens/hundreds) to show a stable, reusable navigation pattern
worth compressing into a skill. Revisit once the follow-up large-scale gates below have run.

## Remaining failure modes (prioritized)

1. **Cross-site structured fact passing is unreliable.** Qwen3-8B's `finish` JSON often
   reports `"facts": {}` even when explicitly instructed to populate a concrete value the
   objective just asked it to find (Section 33/34's own worked example: "find the project
   code, then enter it into configuration" — the code was found and verified, but not always
   captured into `facts`, so the dependent step never received it and timed out at the step
   budget). This is the leading blocker on ordered-workflow reliability, not verification or
   navigation. Candidate fix (not implemented this pass, would need its own validation round):
   a second, small, tightly-scoped extraction call specifically for `facts`, separate from the
   free-form `finish` JSON, so it isn't competing for the model's attention with `summary`/
   `evidence`/`verified`.
2. **Free-form JSON-inside-JSON is fragile.** The `finish` action's `result` field is a plain
   string the model must itself fill with valid JSON; it isn't grammar-constrained at that
   inner level, so a literal quote inside `evidence` broke JSON parsing in one of the first
   two workflow trials run in this pass (see "Multi-site action workflows" below). An explicit
   "no literal quotes in string values" instruction measurably fixed this failure mode in the
   very next run (both re-run trials this triggered on since were clean) — worth watching, not
   yet proven durable at scale.
3. **Research source discovery cannot yet reliably enumerate multiple links from one
   results page.** After fixing the truncation issue above, the discovery `AgentLoop` got
   stuck repeatedly `extract`-ing the same single search-result element rather than reading
   several links and finishing — 0/2 live attempts produced a usable source list. Candidate
   fix: request a full-page `extract` (no target — returns the whole page's visible text at
   once, already supported by the existing action vocabulary) instead of relying on the model
   to iterate single-element extracts, so multiple result URLs are visible in one observation.
4. **Deeply ambiguous prompts route inconsistently by construction, not by defect.** The
   router's schema-validity and no-hallucinated-target guarantees held 100% (15/15) on a live
   sample of maximally ambiguous prompts ("Do the weekly check," "Handle it the way you
   normally would") — but several were classified `single_site`/`multisite_sweep` with an
   empty target list, which is internally consistent (nothing to hallucinate) but not
   *executable* as-is. There is no ground truth for what these prompts "should" route to; real
   usage will supply enough context (a URL, a clearer verb) that this bucket should be rare.
5. **A pytest-under-Windows test-runner flakiness**, not reproduced in the underlying browser
   backend when driven directly — see "Tests" above. Worth a follow-up investigation if it
   recurs in CI, but did not block validating the actual code paths.

## Next phase (evidence-driven recommendation only — not implemented)

Run the deferred full-scale gates before calling ordered-workflow/router capability fully
validated:

```powershell
# Full 10+10 (tuned+holdout) ordered-workflow trial gate (this pass ran 3 tuned + cross-site
# dependency scenario only; extend benchmarks/run_phase5b_workflow_trials.py's _scenarios()
# with 7 more tuned + 9 more holdout variants first)
python benchmarks/run_phase5b_workflow_trials.py

# Full 50-prompt router benchmark (this pass's tests/unit/test_router.py already covers the
# deterministic-obvious slice at 100%; extend benchmarks/run_phase5b_router_live.py's PROMPTS
# list for the remaining live-fallback prompts to reach 50 total)
python benchmarks/run_phase5b_router_live.py

# 20-30-site public pilot (extend benchmarks/run_phase5_public_web_pilot.py's target list)
python benchmarks/run_phase5_public_web_pilot.py
```

Research discovery re-validation, after implementing the full-page-extract fix above: submit
a research prompt via `browser-agent ui` and confirm `ui/jobs.py::_discover_sources` returns
a non-empty URL list within the search step budget.

Then, once the user is available: walk through one real authenticated LMS site in read-only
mode via `browser-agent ui`, using the manual-login-handoff flow documented above, before
scaling to 2-3 then more sites (Section 51-52).

Priority order for the next implementation pass (not scale, capability): (1) cross-site
fact-passing, (2) research-discovery multi-link enumeration — both are instances of the same
underlying pattern (a free-form response competing against a structured sub-field) and a
shared fix approach (a separate, smaller, constrained extraction call) is worth validating
against both at once.
