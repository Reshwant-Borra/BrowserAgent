# Semantic Task Planner

Design doc for the control-plane correction on top of `browseragent-v1-ready`: replacing the
brittle keyword/regex-first natural-language router's *failure mode* — "no targets found in
the task text" on a perfectly reasonable request like "Check all my course pages and tell me
what I still need to do this week" — with a schema-constrained semantic planner + a
deterministic resource resolver, while keeping every execution engine (`AgentLoop`,
`BatchOrchestrator`, `WorkflowOrchestrator`, `research/discovery.py`) completely unchanged.

## 1. Motivation

`router/extract.py`'s deterministic router and `router/llm_router.py`'s Qwen fallback both
share one structural limitation: they can only route a target that's *already spelled out* as
a literal URL in the prompt text. A prompt that refers to a resource *semantically* — "my
course pages," "the tabs I have open," "this page" — has no literal URL for either path to
find, so `router.schema.RouterDecision.targets` comes back empty, and
`ui/jobs.py::_run_sweep` hits its `if not decision.targets: fail("no targets found...")`
guard. The router isn't wrong that it can't find a URL — it's wrong that "I can't find a URL"
was ever treated as a terminal failure instead of a question worth asking.

The fix isn't "add more keywords" (a `if "course" in prompt` branch just moves the brittleness
one line down and needs endless maintenance for every phrasing a user might use). The fix is
letting Qwen do what it's actually good at — understanding what a sentence *means* — while
keeping every part of the system that decides what's *real* (which URLs exist, which tabs are
open, what's safe to do) entirely deterministic, exactly as before.

## 2. Architecture

```
old:  USER PROMPT -> keyword router -> fixed task type -> execution
new:  USER PROMPT -> semantic planner -> structured plan -> resource resolver
                                                           -> validated RouterDecision
                                                           -> existing execution engines
```

```
router/policy.py::route(prompt, client, config)
  1. router/extract.py::try_deterministic_route()      <- unchanged, still first, still free
     (obvious literal-URL shapes skip everything below entirely)
  2. router/semantic_planner.py::plan_task()            <- NEW: Qwen, schema-constrained
     -> router/plan_schema.py::TaskPlan
  3. router/policy.py::_translate_plan()                <- NEW: deterministic translation
     -> router/resources.py::ResourceResolver           <- NEW: deterministic resolution
        - explicit_urls -> router/extract.py::extract_urls() (already-existing code)
        - current_page  -> empty targets (AgentLoop's existing "use whatever's open" path)
        - open_tabs     -> browser/tabs.py::list_open_tabs() + Qwen id-selection
        - web_discovery -> nothing to resolve; execution_shape=open_research does the rest
     -> router.schema.RouterDecision  (existing shape, unchanged)
        | router.policy.NeedsInput   (NEW: "ask the user" instead of "fail")
  4. (fallback, "hybrid"/"legacy" modes only) router/llm_router.py::route_with_model()
```

`AgentLoop`, `BatchOrchestrator`, `WorkflowOrchestrator`, and `research/discovery.py` never
see a `TaskPlan` — they only ever consume a `RouterDecision`, the exact same type the old
router already produced. `ui/jobs.py::_drive()`'s dispatch on `decision.task_type` is
completely unchanged. The entire correction lives in `router/` (plus the small `browser/tabs.py`
addition and the `ui/jobs.py` clarification loop) — see docs/BROWSERAGENT_MASTER_STATUS.md
Section 4 for how this fits the rest of the system.

## 3. Legacy Router's Role

`router/extract.py::try_deterministic_route()` is unchanged and still runs first, unconditionally,
in every mode — it's a pure optimization (near-zero cost, no model call) for prompts whose shape
really is unambiguous from literal URLs alone ("Open https://x", "Check A and B"). It was never
the thing that needed fixing; the *fallback* was.

`router/llm_router.py::route_with_model()` (the old, keyword-adjacent Qwen router — one JSON
call, no resource resolution, no clarification) is kept, unmodified, as a fallback governed by
`config.routing.mode`:

| Mode | Deterministic fast path | Then | Fallback |
|---|---|---|---|
| `legacy` | yes | `route_with_model()` only | — (pre-existing behavior, exactly) |
| `semantic` | yes | planner + resolver only | none — planner failure raises `RoutingError` |
| `hybrid` (default) | yes | planner + resolver | `route_with_model()`, only if planning/translation itself errors (never for an unresolvable resource — that's `NeedsInput`, not an error) |

Passing `config=None` to `router.policy.route()` (as `benchmarks/run_phase5b_assignment_sweep_live.py`
still does) reproduces `legacy` mode exactly — a caller has to explicitly pass a config to opt
into the planner at all. Nothing that hasn't been touched by this pass has its behavior changed.

## 4. Plan Schema

`router/plan_schema.py`, mirrors `router/schema.py::RouterDecision`'s "never trusted prose"
philosophy — everything below is a Pydantic model the model's raw JSON is validated against
before any code trusts it:

```
TaskPlan
  goal: str
  intent: read | act | research | mixed
  execution_shape: single | sweep | ordered_workflow | open_research   (1:1 with TaskType)
  resource_requirements: [ResourceRequirement]
  steps: [PlannedStep]              # only meaningful for ordered_workflow
  constraints: {read_only, navigation_scope}
  result_contract: generic | assignment | research

ResourceRequirement
  kind: explicit_urls | current_page | open_tabs | web_discovery
  description: str      # semantic hint only, e.g. "the user's course pages" — NEVER a URL

PlannedStep
  ordinal: int
  resource_ref: int     # index into resource_requirements
  objective: str
```

The planner has no field anywhere it could put a URL in. `ResourceRequirement.description` is
free text used only as (a) a resolver-strategy hint and (b) the filtering objective handed to
the *separate* open-tab selection call — it is never treated as, or parsed for, a URL. This is
a structural guarantee, not a runtime check: hallucinating a URL is not representable in the
schema at all.

## 5. Resource Resolver

`router/resources.py::ResourceResolver` is the only place a `ResourceRequirement` becomes a
real URL:

- **`explicit_urls`** — `router/extract.py::extract_urls()` on the actual prompt text (the
  same function the deterministic router already used). For `ordered_workflow` plans with more
  than one `explicit_urls` requirement, `router/policy.py::_assign_explicit_urls()` assigns the
  *k*-th URL found in the text to the *k*-th `explicit_urls` requirement, in `resource_requirements`
  order — the natural fix for "site A ... then ... site B" phrasing, where resolving each
  requirement independently would otherwise hand every step the same full URL list.
- **`current_page`** — resolves to empty targets. This isn't a stub: `AgentLoop` already treats
  an empty-target `single_site` task as "act on whatever page is already attached" (proven
  working end-to-end in the CDP-attach persistence validation, see
  docs/BROWSERAGENT_MASTER_STATUS.md Section 21) — there was nothing to build here.
- **`open_tabs`** — `browser/tabs.py::list_open_tabs()` enumerates the attached browser's real
  tabs via the Chrome DevTools HTTP API (`GET {cdp_endpoint}/json/list`, no full Playwright
  connection needed just to read titles/URLs), then `router/resources.py::select_relevant_tabs()`
  asks Qwen to pick relevant tab **ids** — never URLs — against the description from the plan.
  Any id outside the candidate set is dropped, same anti-hallucination pattern
  `research/discovery.py::select_relevant_links()` already established for search-result
  selection. Only available when `browser.mode == "cdp_attach"` (see Section 8).
- **`web_discovery`** — not resolved here at all; `_translate_plan()` maps `open_research`
  directly to `RouterDecision(task_type=RESEARCH, requires_discovery=True, targets=[])`,
  exactly what the router already produced for research prompts — `research/discovery.py`'s
  existing deterministic-candidate-enumeration + id-selection pipeline runs completely
  unchanged downstream in `ui/jobs.py::_run_research`.

A resource that resolves to nothing becomes an entry in `ResourceResolution`'s unresolved set;
`_translate_plan()` turns that into `NeedsInput`, never an empty-target `RouterDecision`.

## 6. Open-Tab Resolution

```
browser/tabs.py::list_open_tabs(cdp_endpoint)
  -> GET {cdp_endpoint}/json/list  (Chrome DevTools HTTP API — no Playwright connection)
  -> filter to type=="page" and http(s):// URLs (drops blank tabs, extensions, chrome://*)
  -> [TabCandidate(id, title, url)]   (sequential synthetic ids, not CDP's own target ids)

router/resources.py::select_relevant_tabs(client, description, tabs)
  -> Qwen, schema-constrained: {"selected_ids": [...]}
  -> any id not in the candidate set is dropped, never trusted
  -> resolver maps the surviving ids back to their real (resolver-owned) URLs
```

Qwen only ever sees `[id] "title" -> url` lines and returns ids. It never rewrites, retypes,
or invents a URL — the exact same shape `research/discovery.py`'s link selection already uses
for search-result candidates, applied to the browser's own open tabs instead of a search page's
links.

## 7. Current-Page Resolution

No enumeration, no model call, no `ResourceRequirement` even needs to resolve to anything:
`execution_shape=single` with an empty (or `current_page`-only) `resource_requirements` list
translates straight to `RouterDecision(task_type=SINGLE_SITE, targets=[])`, and `AgentLoop`
does what it already does for that shape — observe whatever page is currently attached (in
`cdp_attach` mode, the user's actual open tab) and act on it.

## 8. Clarification Flow

`router.policy.NeedsInput(question, plan)` replaces the old failure mode. `ui/jobs.py`'s
`_route_with_clarification()`:

1. Routes the prompt; if the result is `NeedsInput`, persists job status `waiting_for_input`
   with `pending_clarification={"question": ...}` and waits (via a new `clarification_future`
   on `_JobControl`, same pattern as the existing approval/login futures).
2. The user answers through a new UI text box (`POST /api/jobs/{id}/clarify`); the answer is
   appended to the *original* prompt (`router.policy.route_with_answer()`) and the whole
   pipeline re-runs from the top — deterministic fast path first, so a pasted URL usually
   resolves immediately without a second planner call at all.
3. Bounded by `MAX_CLARIFICATION_ROUNDS = 3` (`ui/jobs.py`) — after that, the job fails cleanly
   rather than asking indefinitely.

Stopping a job while it's waiting for input resolves the clarification future with `None`,
exactly mirroring how `stop()` already unblocks a pending approval or login wait.

## 9. Dynamic Replanning

Deliberately narrow, per the task's own "not a continuous loop" requirement:
`router/replanner.py::decide_replan()` is called **at most once**, only after a `sweep`-shaped
job finishes with structured findings, and only when the plan's `intent` was `mixed`
(`RouterDecision.mixed_intent_followup`, set by `_translate_sweep()`). One schema-constrained
call (`ReplanDecision`) decides `continue | revise | clarify | finish`; `revise` selects a
finding **by id** (never a re-typed URL, same pattern as everywhere else in this design) and
`ui/jobs.py::_maybe_replan()` runs it as one follow-up `single_site` job phase through the
existing `_run_single()` — no second orchestration engine, no loop.

## 10. Safety

Nothing here bypasses anything. `PlanConstraints.read_only` only sets the *default*
`preferred_policy` on the translated `RouterDecision` — exactly the same caveat
`router/schema.py::SafetyPolicy` already carried before this pass. Real consequential-action
gating (`agent/schemas.py::classify_risk()`), the approval flow, `NavigationScope`
enforcement, and verified-workflow-fact constraints are all completely independent of
anything the planner outputs and are untouched by this change.

## 11. Benchmarks

`benchmarks/run_semantic_planner_live.py` — real Qwen3-8B/Ollama, 128 prompts (104 tuned + 24
holdout, unseen phrasings) across 8 categories (single_explicit, current_page, sweep_explicit,
open_tabs, ordered_workflow, mixed_intent, research, clarification). Plan-shape categories
grade `plan_task()`'s raw output directly; `open_tabs`/`clarification` grade the full `route()`
pipeline against a synthetic open-tab pool (`router.resources.list_open_tabs` patched, no real
browser needed) so hallucination and clarification behavior are checked end to end without
depending on a live Chrome instance.

Final result: **98.4% overall accuracy (98.1% tuned / 100% holdout), 100% schema validity,
0 hallucinated URLs/resources** — see docs/BROWSERAGENT_MASTER_STATUS.md Section 6 for the
full breakdown and the two real bugs this benchmark caught and fixed (a sweep-vs-
ordered_workflow classification confusion, and an overly-inclusive open-tab selection default
that this pass tightened to fail toward clarification rather than a wrong guess).

## 12. Known Limitations

- Open-tab "most relevant" selection is bounded by whatever's actually open — a course page
  the user hasn't opened yet still correctly triggers clarification, not a guess. It is not
  perfect: the live benchmark showed a small (~1/13) miss rate in both directions before
  tuning (over- and under-inclusive) and a residual ~1/13 miss rate after tuning it toward the
  safer (clarify-when-unsure) direction — see Section 11's numbers.
- `research/discovery.py` is still deliberately isolated from `cdp_attach` mode (unchanged
  from the prior pass) — `web_discovery` resource requirements never touch the user's
  attached tabs.
- Replanning is a single bounded round; a task that needs a second deterministic follow-up
  (e.g. "then check whether that assignment allows a late submission") is out of scope and
  would need the user to ask again.
- The semantic planner adds one extra model call (occasionally two, for `open_tabs`/research
  resolution sub-calls) versus the deterministic fast path — see
  docs/BROWSERAGENT_MASTER_STATUS.md Section 6 for measured overhead.
- Observed during real-tab E2E validation (unrelated to this pass, pre-existing in
  `PlaywrightBackend`'s `cdp_attach` page-selection policy): a batch/workflow child task
  reuses whichever tab was last attached and navigates it to its own target via `open_url`,
  rather than opening one dedicated tab per work item. This doesn't affect correctness (each
  item still ends up reading/acting on the right target URL), but it means a batch sweep over
  N targets in `cdp_attach` mode can end up "borrowing" and renavigating one of the user's
  other open tabs rather than leaving all N of them untouched. Not fixed in this pass — it is
  `PlaywrightBackend`/page-selection-policy territory, out of scope for a control-plane-only
  correction (see the task's own "do not rewrite AgentLoop/PlaywrightBackend" instruction).
