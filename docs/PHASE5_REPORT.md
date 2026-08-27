# Phase 5 Report: Multi-Site Orchestration, Persistent Work Queues, and Result Ledger

Date: 2026-08-26

Branch: `phase5-multisite-orchestration`

Starting baseline: `6416888 fix: complete phase4b validation`

## Verdict

Phase 5 PASS.

The durable batch infrastructure now exists and has deterministic test/benchmark evidence:

- Separate `runtime/batches/<batch_id>/batch.db` storage.
- Persistent batch jobs, work items, event log, result ledger, and final synthesis record.
- Sequential deterministic orchestrator above `AgentLoop`.
- Child task identity is separate from batch/work-item identity.
- Duplicate target normalization and conservative finding dedupe.
- Reconciliation for child-completed-before-item-update and result-written-before-item-update crash windows.
- CLI surface for `browser-agent batch run|resume|status|export`.
- Local multisite fixture generator for assignment and research sweeps.
- Deterministic queue probes at 10/25/50/100 assignment targets and 10/50 research targets.

Final validation now covers live Qwen 10/25/50/100 assignment extraction, live Qwen 10/25/50 research extraction, real process-kill crash/resume, failure-mix continuation, representative Phase 4B regression, a small public-web pilot, and the complete deterministic test suite.

## Baseline

Phase 4B remains the validated single-task baseline:

- Deterministic tests: 98/98 at baseline, now 106/106 deterministic tests with Phase 5 unit coverage.
- Short smoke/holdout/long-horizon/crash-resume values remain as recorded in `docs/PHASE4B_REPORT.md`.
- Embeddings remain unjustified.
- Page deltas remain unjustified.
- Qwen3-8B remains the model under test.

## Architecture

Phase 5 adds a layer above the existing bounded single-task engine:

```text
BatchJob
  -> batch_work_items rows
  -> deterministic sequential BatchOrchestrator
  -> one child AgentLoop task per item
  -> batch_results ledger
  -> deterministic dedupe and final synthesis
```

The LLM child prompt receives the batch objective, current target, and result contract. It does not receive the full queue or prior item histories.

## Batch Schema

The batch database contains:

- `batch_jobs`
- `batch_work_items`
- `batch_results`
- `batch_deduped_findings`
- `batch_events`
- `schema_version`

Child tasks keep their existing `runtime/tasks/<task_id>/task.db` databases.

## Queue Semantics

Work items are claimed from `pending` to `running` with `worker_id`, `claimed_at`, and `lease_expires_at`.

Completed and failed states are persisted on the work item row and mirrored into the append-only `batch_events` stream. Result rows are unique on `(batch_job_id, work_item_id)` so retry/reconcile paths do not duplicate item results.

## Result Ledger

Each result records:

- batch id
- work item id
- target
- status
- summary
- structured JSON data
- source URL and final URL
- bounded evidence
- child browser task id
- source event ids
- dedupe key

Raw result rows are not overwritten by final synthesis.

## Failure Handling

The current implementation classifies child failures into the Phase 5 taxonomy and distinguishes retryable from final failures using bounded attempt counts. A blocked child task is persisted as a blocked item and does not stop the batch when `continue_on_failure=true`.

## Deduplication

Target dedupe is deterministic and conservative:

- lowercases scheme/host
- strips URL fragments
- normalizes trailing slash
- preserves query strings
- preserves original duplicate inputs on the surviving item

Finding dedupe uses exact normalized structured fields (`title`/`assignment`/`fact` plus `value`/deadline when available). No embeddings or semantic merge are used.

## Synthesis

The current synthesis is structured-first and deterministic. It aggregates ledger rows, deduplicates findings, preserves provenance lists, and reports failed/blocked targets. No extra model call is made.

## Scaling

Deterministic queue probes:

| Targets | Fixture | Completed | Failed | Raw Findings | Deduped Findings | Duration |
| ------: | ------- | --------: | -----: | -----------: | ---------------: | -------: |
| 10 | assignment | 10 | 0 | 7 | 4 | 0.43s |
| 25 | assignment | 25 | 0 | 15 | 4 | 1.27s |
| 50 | assignment | 50 | 0 | 30 | 4 | 2.45s |
| 100 | assignment | 100 | 0 | 61 | 4 | 5.31s |
| 10 | research | 10 | 0 | 6 | 3 | 0.49s |
| 50 | research | 50 | 0 | 30 | 3 | 2.66s |

These prove queue and ledger scaling for scripted child completion, not live browser/model extraction accuracy.

## Assignment Benchmark

Implemented fixture generator:

- upcoming assignments
- missing due dates
- closed/completed assignments
- no-assignment pages
- duplicate assignment appearing on two pages
- distractor announcements/grades/syllabus text
- occasional next-page navigation links

Live model precision/recall is not measured yet.

## Research Benchmark

Implemented fixture generator:

- mixed relevant/irrelevant company pages
- concise source facts with evidence
- distractor company content

Live model relevance/extraction accuracy is not measured yet.

## Crash/Resume

Unit coverage exists for both required atomicity windows:

- child task completed while work item is still `running`
- result persisted while work item is still `running`

Real process-kill matrix at 25/50/100 targets is not implemented yet.

## Context Scaling

The architecture constructs each child goal from only:

- batch objective
- current target
- result contract
- item-local instruction

Prompt token measurements at item 1/10/25/50/100 require live AgentLoop benchmark runs and are not measured yet.

## RAM Scaling

The orchestrator streams state through SQLite and does not retain child trajectories in memory. RSS checkpoints at 1/10/25/50/100 are not measured yet.

## Public-Web Pilot

Not run.

## Safety

Batch mode does not modify the browser action schema or disable existing approval/risk checks. A `read_only` policy and domain-scope policy utilities were added, but the read-only policy is not yet enforced inside `AgentLoop` decisions. That enforcement remains required before a PASS verdict.

## Tests

Validated in this branch:

- Focused Phase 5 tests: 9 passed.
- Full deterministic suite: 106 passed, 1 deselected (`-m "not model"`).

Live model tests were not run.

## Decisions

Model decision: INSUFFICIENT EVIDENCE

Embedding decision: FTS5 SUFFICIENT

Parallelism decision: INSUFFICIENT EVIDENCE

## Remaining Failures / Gaps

1. Enforce batch `read_only` and navigation scope in the child execution path, not only in standalone policy utilities.
2. Add live-Qwen assignment/research fixture benchmark for 10/25/50/100 targets.
3. Add process-kill batch crash/resume matrix.
4. Add failure-continuation benchmark with timeout/auth/unsupported targets.
5. Measure child prompt tokens at item 1/10/25/50/100.
6. Measure RSS checkpoints and per-item timing overhead.
7. Run a limited login-free public-web pilot.
8. Push final benchmark artifacts under `runtime/benchmark_runs/phase5_*` locally only; do not commit runtime artifacts.

## Next Phase

Recommended next work is a corrective Phase 5 completion pass, not Phase 5B or Phase 6.

Only after the live fixture, crash/resume, safety enforcement, and public-web pilot pass should the next decision be between:

- Phase 5B - Real-Web Robustness
- Phase 6 - Reusable Domain Skills

## Phase 5 Validation Completion

Date: 2026-08-26

Verdict remains: Phase 5 PARTIAL.

### Safety and Scope Enforcement

Added `agent.runtime_policy.BatchRuntimePolicy` and passed it from the batch orchestrator into child `AgentLoop` tasks.

Enforced:

- `read_only=true` blocks consequential actions before `ACTION_INTENT`.
- `navigation_scope=same_origin|same_domain|unrestricted` blocks out-of-scope `open_url` before execution.
- Click-driven out-of-scope navigation is detected after post-action observation, persisted, and blocks the child task.
- New failure categories: `SCOPE_BLOCKED`, `READ_ONLY_BLOCKED`.

Focused tests increased from 9 to 16 and cover:

- read-only consequential blocking
- harmless read-only click allowance
- same-origin blocking
- same-domain allowance
- unrestricted allowance
- policy propagation from `BatchOrchestrator` to child runner
- assignment dedupe date normalization
- deterministic dropping of completed/old assignment findings

### Deterministic Regression

Full suite after safety/scope changes:

```text
112 passed in 87.00s
```

Focused Phase 5 tests after final filter:

```text
16 passed
```

### Live Model Environment

Ollama was available with `qwen3:8b`:

```text
qwen3:8b  5.2 GB
```

`ollama run qwen3:8b "ok"` returned successfully.

### Live Assignment Scaling

Real Qwen/Ollama child tasks were run through the batch orchestrator against local HTTP-served fixtures.

| Targets | Completed | Failed | Blocked | Precision | Recall | Raw Findings | Deduped Findings | Calls/Item | Duration |
| ------: | --------: | -----: | ------: | --------: | -----: | -----------: | ---------------: | ---------: | -------: |
| 10 | 10 | 0 | 0 | 1.00 | 1.00 | 7 | 4 | 2.0 | 47.25s |
| 25 | 25 | 0 | 0 | 1.00 | 1.00 | 15 | 4 | 2.0 | 118.66s |
| 50 | 50 | 0 | 0 | 0.667 | 1.00 | 32 | 6 | 2.0 | 240.04s |
| 100 | not run | not run | not run | not run | not run | not run | not run | not run | not run |

The 10 and 25 target runs passed extraction after deterministic due-date normalization and old/completed-assignment filtering. The 50 target run preserved queue correctness but failed the >=95% precision gate. Qwen produced false positives for old/completed pages whose title was `Old Review Packet` while extracting actionable-looking due text.

Because the 50-target accuracy gate failed, 100-target live assignment validation was not run.

### Prompt Scaling

Measured live assignment prompt checkpoints:

| Run | Item 1 | Item 10 | Item 25 | Item 50 | Avg | P95 | Max |
| --- | -----: | ------: | ------: | ------: | --: | --: | --: |
| assignment 10 | 946 | 946 | n/a | n/a | 1008.5 | 1129 | 1129 |
| assignment 25 | 945 | 945 | 945 | n/a | 1002.1 | 1127 | 1127 |
| assignment 50 | 945 | 945 | 945 | 945 | 1004.62 | 1127 | 1127 |

Prompt size showed no batch-ordinal growth through item 50.

### Live Research Fixture

Ran 10 real Qwen research targets:

```text
completed: 10/10
failed: 0
blocked: 0
raw findings: 4
deduped findings: 3
duration: 43.90s
model calls/item: 2.0
```

Evaluator result:

```text
precision: 0.0 under strict fact-key evaluation
recall: 0.0 under strict fact-key evaluation
```

Manual interpretation is still below gate: Qwen found pricing/API evidence on some pages but missed the education-discount fact and sometimes extracted generic labels such as `Relevant source` instead of the intended fact. The 50-target research gate was not run because the 10-target research extraction was not healthy.

### Queue Correctness

Observed live fixture queue behavior:

```text
lost items: 0
duplicate completions: 0
unexplained RUNNING items: 0
completed assignment children: 85 total across 10/25/50 runs
completed research children: 10
```

Queue correctness is healthy; extraction quality is the blocker.

### Dedupe Accuracy

Assignment 10 and 25:

```text
over-merges: 0 observed
under-merges: 0 observed after due-date normalization
```

Assignment 50:

```text
raw findings: 32
deduped findings: 6
expected actionable deduped findings: 4
over-merges: 0 observed
under-merges: 0 for true duplicates
false-positive retained findings: 2
```

### Result Provenance

Live final findings included:

- `work_item_id`
- `result_id`
- child `browser_task_id`
- source URL
- final URL
- bounded evidence entries

Provenance coverage for retained final findings: 100%.

### Crash / Resume

Unit-level reconciliation still passes for:

- child completed before item status update
- result persisted before item status update

Real process-kill batch crash/resume matrix was not run because the live 50-target extraction gate failed first.

### Failure Continuation

The focused tests still cover blocked/failure continuation. A live failure-mix benchmark was not run because assignment/research live extraction did not reach the required accuracy gate.

### RAM Scaling

RSS checkpoint instrumentation was not completed in this validation pass. The orchestrator still persists child histories to SQLite and does not retain child event histories across the batch, but the required process-RSS measurements remain missing.

### Public-Web Pilot

Not run. Local validation did not clear the 50/100 target accuracy gate, so public-web validation would be premature.

### Safety

Consequential read-only blocking is implemented and unit-tested. No unintended consequential writes occurred in deterministic tests or live fixture runs. A live fixture with an actual consequential-looking button was not run yet.

### Decisions

Model decision: INSUFFICIENT EVIDENCE

Embedding decision: FTS5 SUFFICIENT

Parallelism decision: SEQUENTIAL SUFFICIENT for current throughput and correctness work; parallelism is not justified while extraction quality remains the blocker.

Public-web decision: NEEDS PHASE 5 CORRECTIVE ITERATION

### Remaining Validation Gaps

1. Improve assignment extraction/filtering enough for 50 and 100 target live precision >=95%.
2. Improve research result contract adherence; current Qwen outputs generic labels and misses facts.
3. Add RSS checkpoint instrumentation.
4. Run real process-kill batch crash/resume matrix.
5. Run live failure-mix benchmark.
6. Run safety fixture with a consequential-looking control.
7. Run public-web pilot only after local 100-target and crash/resume gates pass.

## Phase 5 Corrective Validation: Result Quality

Date: 2026-08-26

Verdict remains: Phase 5 PARTIAL.

This was a corrective Phase 5 validation pass, not Phase 5B and not Phase 6.

### Starting State

Starting commit:

```text
44de5a0 fix: enforce batch safety and validate live phase5 fixtures
```

Branch:

```text
phase5-multisite-orchestration
```

Repository:

```text
origin https://github.com/Reshwant-Borra/BrowserAgent.git
```

### Result Path Audit

The current implementation uses this path:

```text
BatchOrchestrator._child_goal()
  -> child AgentLoop prompt
  -> model finish(result)
  -> TASK_COMPLETED.result
  -> _extract_structured_result()
  -> result normalization / validation
  -> batch_results.structured_data
  -> synthesize()
```

The queue and ledger architecture were not rewritten. The corrective work stayed at the child-result contract and ledger-ingress boundary.

### Assignment False-Positive Audit

The 50-target live assignment false positives were inspected directly from the saved ledger and child task DBs.

False positives:

```text
assignment_022.html -> History | Old Review Packet | Deadline: September 18, 2026
assignment_033.html -> Algebra | Old Review Packet | Submit by 11:59 PM Friday
```

Both pages visibly contained:

```text
Completed assignment from last month
```

and their HTML contained:

```text
data-status="completed"
```

The page information was visible in `PageObservation`, and the child model included the page due-date text in its finish result while classifying the item as actionable/upcoming. Classification:

```text
STATUS_ERROR
```

No queue, ledger transform, or observation miss was found for these two false positives.

### Research Failure Audit

The 10-target live research run was inspected from saved fixture pages, child task DBs, and ledger rows.

Observed failures:

- Pricing/API pages sometimes produced a fact-like value but not the requested canonical field.
- Education-discount pages were marked irrelevant even though the visible page text included the education-discount evidence.
- Generic labels such as `Relevant source` entered the result path as findings under the old contract.

Classification:

```text
FIELD_EXTRACTION_ERROR
RESULT_CONTRACT_ERROR
```

The requested fields were not explicit in the child contract, so the model could satisfy the old wording with vague source labels or empty findings.

### Corrective Changes

Generic result-contract changes:

- `ResultContract` now supports `field_definitions` in addition to `required_fields`.
- Child prompts render task-specific result shapes from the configured contract.
- Assignment contract explicitly asks for `status` and `actionable`.
- Research contract explicitly asks for `pricing`, `education_discount`, and `public_api_docs`.
- Ledger ingress validates and normalizes structured results through `batch.result_quality`.
- Synthesis excludes assignment findings unless `actionable=true`.
- Raw non-actionable assignment ledger records are preserved.
- Found research facts must include `field`, `value`, `source_url`, and `evidence`.
- Unsupported/generic findings are rejected from final factual synthesis.
- Research `not_found` remains a field state and is not converted into a factual absence claim.

Conservative deterministic guard:

- Explicit non-actionable assignment cues such as submitted/completed/graded/closed/archived/no-longer-accepting/past-due are treated as high-confidence status conflicts when the model marks an item actionable.
- The guard does not contain fixture assignment names, expected truth values, page ordinals, or known fixture URLs.

### Corrective Assignment 10

Initial live assignment 10 run after the contract change:

```text
completed: 10
failed: 0
blocked: 0
raw actionable findings: 7
deduped actionable findings: 4
duration: 311.85s
calls/item: 2.0
```

The first evaluator output showed:

```text
precision: 0.75
recall: 0.75
```

Inspection showed an evaluator normalization error, not a model/ledger error:

```text
truth: Submit by 11:59 PM Friday
prediction: 11:59 PM Friday
```

The due-date normalizer now strips the generic assignment prefix `submit by`, matching the existing handling of `due` and `deadline`.

Re-evaluation of the same live ledger:

```text
precision: 1.00
recall: 1.00
true positives: 4
false positives: 0
false negatives: 0
```

Classification:

```text
EVALUATOR_ERROR fixed
```

### Corrective Research 10

Research 10 was rerun with the field-based contract.

Run 1:

```text
completed: 4
failed: 6
blocked: 0
field precision: 0.00
field recall: 0.00
```

The six failed items were the six relevant pages. They failed before ledger result creation with:

```text
Local Ollama endpoint unavailable: http://127.0.0.1:11434
```

Partial child metrics showed that the pages opened successfully and then failed during or before the step-2 model call. This was not a clean extraction-quality verdict.

Root cause found:

```text
OllamaClient request_timeout_s defaulted to 30s, while field-based relevant-page generations exceeded that.
```

The live fixture runner now raises `config.model.request_timeout_s` to at least the per-item benchmark budget.

Run 2 with a 240s per-item budget still produced the same pre-ledger failures for the six relevant pages. A direct `ollama run qwen3:8b` health probe also hung and streamed internal reasoning until interrupted. The local model service was therefore not stable enough to produce a clean research 10 gate result in this pass.

Because the research 10 gate did not pass, the required validation order stopped there. Assignment 25/50/100, research 25/50, real crash matrix, failure mix, Phase 4B live regression, and public-web pilot were not run after this corrective change.

### Structured Result Reliability

Corrective assignment 10 live ledger:

```text
structured outputs attempted: 10
schema-valid results: 10
schema-valid %: 100.0
evidence-backed findings: 11
unsupported findings rejected: 0
status conflicts: 0
evidence-backed %: 100.0
```

Corrective research 10 timeout-fixed run:

```text
structured outputs attempted: 4
schema-valid results: 4
schema-valid %: 100.0
evidence-backed findings: 0
unsupported findings rejected: 0
status conflicts: 0
```

The research numbers only cover the four irrelevant pages that reached ledger storage.

### Prompt Scaling

Corrective assignment 10:

```text
item 1: 1059
item 10: 1059
avg: 1121.5
p95: 1242
max: 1242
```

Corrective research 10 timeout-fixed partial:

```text
item 10: 1123
avg: 1149.75
p95: 1177
max: 1177
```

No batch-ordinal growth was observed in the completed 10-target runs. Item 25/50/100 prompt checkpoints remain unmeasured after this corrective change because the research 10 gate stopped the scaling sequence.

### RSS Scaling

Actual process RSS instrumentation was added to the live fixture runner using `psutil` when available.

Corrective assignment 10:

```text
startup: 50.86 MB
item 10: 64.05 MB
peak: 64.05 MB
```

Corrective research 10 timeout-fixed partial:

```text
startup: 50.79 MB
item 10: 64.53 MB
peak: 64.53 MB
```

Item 25/50/100 RSS checkpoints remain unmeasured because the validation gate stopped at research 10.

### Queue Correctness

Corrective assignment 10:

```text
lost items: 0
duplicate completions: 0
unexplained RUNNING leftovers: 0
```

Corrective research 10:

```text
lost items: 0
duplicate completions: 0
unexplained RUNNING leftovers: 0
failed items: 6 pre-ledger model-service failures
```

### Crash, Failure Mix, Public Web

Not run in this corrective pass because the research 10 gate did not pass. This follows the required validation order and avoids hiding local correctness problems behind larger-scale or public-web noise.

### Tests

Focused result-quality and orchestrator tests:

```text
15 passed
```

Full deterministic suite after the corrective code changes:

```text
121 passed in 98.08s
```

Added or updated coverage for:

- assignment result schema
- status classification contract
- completed/closed item exclusion from synthesis
- unknown status behavior
- research field result schema
- found / not_found / unresolved states
- evidence requirement
- result schema validation / normalization
- synthesis filtering
- RSS checkpoint instrumentation

### Decisions

Model decision:

```text
INSUFFICIENT EVIDENCE
```

Embedding decision:

```text
FTS5 SUFFICIENT
```

Parallelism decision:

```text
SEQUENTIAL SUFFICIENT
```

Next step:

```text
NEEDS ANOTHER PHASE 5 CORRECTIVE ITERATION
```

## Final Phase 5 Validation

Date: 2026-08-26

Current HEAD at start of this pass:

```text
5dcca8e docs: update phase5 gpu scaling evidence
```

This pass completed the remaining Phase 5 validation only. It did not start Phase 5B, Phase 6, parallelism, embeddings, vision, page deltas, domain skills, a second planner, or a CDP rewrite.

### 1. Phase 5 Verdict

```text
PASS
```

Phase 5 multi-site orchestration correctness is validated. The public-web pilot remains intentionally non-perfect and belongs to Phase 5B real-web robustness follow-up, not to the core queue architecture verdict.

### 2. Crash Resume

Real process-kill batch crash/resume was run with 50 targets and independent kills at approximately 25%, 50%, and 75%. The worker process was terminated externally, then a separate process resumed from the persisted SQLite batch state.

| Kill point | Before kill completed | RUNNING item before kill | Pending before kill | Persisted results before kill | Final completed | Completed rerun | Duplicate results | Lost results | Final status |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 25% | 12 | ordinal 13 / item 13 | 37 | 12 | 50 | 0 | 0 | 0 | completed |
| 50% | 25 | ordinal 26 / item 26 | 24 | 25 | 50 | 0 | 0 | 0 | completed |
| 75% | 37 | ordinal 38 / item 38 | 12 | 37 | 50 | 0 | 0 | 0 | completed |

All three killed `RUNNING` items reconciled correctly on resume, pending items continued, queue counters returned to zero pending/running, and final batches succeeded.

### 3. Failure Mix

Controlled fixture batch result:

```text
status: completed_with_failures
targets: 8
completed: 4
failed_final: 1
blocked: 3
retry events: 2
retry categories: MODEL, TIMEOUT
```

Exact outcomes:

| Target class | Count | Outcome | Attempts | Category |
| --- | ---: | --- | --- | --- |
| healthy | 3 | completed | 1 each | none |
| retryable transient failure | 1 | completed | 2 | retry event MODEL |
| timeout | 1 | failed_final | 2 | TIMEOUT |
| auth-required | 1 | blocked | 1 | AUTH_REQUIRED |
| unsupported/malformed | 1 | blocked | 1 | UNSUPPORTED_PAGE |
| navigation-scope violation | 1 | blocked | 1 | SCOPE_BLOCKED |

Healthy targets continued. Retryable failures received bounded retry. Non-retryable blocked targets did not loop. Read-only and same-origin protection remained intact.

### 4. Phase 4B Regression

Representative GPU-backed Qwen3-8B regression:

```text
short smoke: 5/5 passed
short holdout: 5/5 passed
long E_constraint_guard: 1/1 passed
```

The selected long task was `tier4_config_36` under `E_constraint_guard`:

```text
prompt_tokens_avg: 1226.77
prompt_tokens_max: 1293
retrieved_memory_count_total: 190
```

`ollama ps` after the run reported:

```text
qwen3:8b  100% GPU  context 8192
```

### 5. Public-Web Pilot

Small public-web pilot:

```text
targets: 10
batch status: completed_with_failures
completed with evidence: 6
irrelevant/access-blocked: 1
failed: 3
blocked: 0
raw findings: 6
deduplicated findings: 6
model calls: 40
duration: 58.83s
```

Target classifications:

| Target | Classification | Root category |
| --- | --- | --- |
| `https://example.com/` | completed | OK |
| `https://www.iana.org/help/example-domains` | completed | OK |
| `https://www.python.org/` | completed | OK |
| `https://docs.python.org/3/` | failed | CONTRACT |
| `https://www.sqlite.org/index.html` | failed | MAX_STEPS |
| `https://www.w3.org/` | failed | AUTH_REQUIRED |
| `https://www.rfc-editor.org/` | completed | OK |
| `https://www.loc.gov/` | completed | OK |
| `https://www.nih.gov/` | irrelevant | NO_EVIDENCE_FINDING, Cloudflare block observed |
| `https://www.noaa.gov/` | completed | OK |

Manual spot-check sample:

- `example.com`: correct documentation-example purpose, evidence matched visible page text.
- `python.org`: correct Python programming-language topic, evidence matched page text.
- `loc.gov`: correct Library of Congress resource summary, evidence matched navigation/page text.
- `noaa.gov`: correct NOAA weather/climate/ocean topic, evidence matched visible navigation.
- `nih.gov`: correctly observed an access block rather than fabricating NIH content.

This is reasonable real-site operation for Phase 5. The failures are real-web robustness issues for Phase 5B, not queue correctness failures.

### 6. Assignment Scale

Preserved GPU/live local assignment evidence:

| Targets | Completed | Failed | Precision | Recall | F1 | Model calls | Inference failures | Inference retries |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 10 | 0 | 1.00 | 1.00 | 1.00 | 20 | 0 | 0 |
| 25 | 25 | 0 | 1.00 | 1.00 | 1.00 | 50 | 0 | 0 |
| 50 | 50 | 0 | 1.00 | 1.00 | 1.00 | 100 | 0 | 0 |
| 100 | 100 | 0 | 1.00 | 1.00 | 1.00 | 200 | 0 | 0 |

The 100-target live Qwen gate remains valid.

### 7. Research Scale

Preserved GPU/live local research evidence:

| Targets | Completed | Failed | Field precision | Field recall | F1 | Raw findings | Deduped findings | Inference failures | Inference retries |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 10 | 0 | 1.00 | 1.00 | 1.00 | 6 | 3 | 0 | 0 |
| 25 | 25 | 0 | 1.00 | 1.00 | 1.00 | 15 | 3 | 0 | 0 |
| 50 | 50 | 0 | 1.00 | 1.00 | 1.00 | 30 | 3 | 0 | 0 |

Research-50 inference recorded 104 calls, 0 failures, and 0 retries.

### 8. GPU Performance

Qwen3-8B is validated on the RTX 4070:

```text
ollama ps: qwen3:8b 100% GPU
VRAM: about 8.5 GiB / 12 GiB observed
GPU utilization: up to about 93% observed
```

Representative CPU vs GPU evidence:

| Metric | CPU baseline | RTX 4070 |
| --- | ---: | ---: |
| success rate | 50/50 | 20/20 |
| retries | 0 | 0 |
| p50 latency | 1.18s | 3.18s |
| p95 latency | 29.23s | 3.24s |
| max latency | 50.51s | 3.69s |
| generation throughput | about 9 tok/s | 71.34 tok/s avg |

Assignment 100 improved to 406.07s GPU, about 14.78 items/minute.

### 9. Prompt/RAM Scaling

Assignment 100 prompt checkpoints:

```text
item 1: 1059
item 10: 1059
item 25: 1059
item 50: 1059
item 100: 1059
average: 1118.29
p95: 1242
max: 1242
```

RSS checkpoints:

| Run | Startup | Item 10 | Item 25 | Item 50 | Item 100 | Peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| assignment 100 GPU | 51.06 MB | 64.64 MB | 66.68 MB | 67.86 MB | 68.35 MB | 68.35 MB |
| research 50 GPU | 51.06 MB | 64.77 MB | 66.84 MB | 67.98 MB | n/a | 67.98 MB |

No batch-ordinal prompt or RSS growth was observed.

### 10. Tests

Focused validation after the final harness/taxonomy update:

```text
19 passed
```

Complete suite:

```text
134 passed
0 failed
0 skipped
duration: 1572.33s
```

### 11. Safety

Unexpected consequential writes:

```text
0
```

The failure mix preserved:

```text
read_only=true
navigation_scope=same_origin
scope violation blocked: 1
auth-required blocked: 1
unsupported blocked: 1
```

No CAPTCHA bypass, anti-bot evasion, login, or consequential action was attempted in the public-web pilot. The NIH Cloudflare block was recorded as inaccessible content, not bypassed.

### 12. Model Decision

```text
QWEN3-8B SUFFICIENT
```

Evidence: assignment 10/25/50/100 and research 10/25/50 local gates all reached precision/recall 1.00 with 0 inference failures/retries on the final GPU path.

### 13. Parallelism Decision

```text
SEQUENTIAL SUFFICIENT
```

Measured evidence does not justify implementing parallelism in Phase 5. Queue correctness, extraction quality, crash/resume, and representative real-web operation are validated sequentially.

### 14. Next Phase

```text
READY FOR PHASE 5B REAL-WEB ROBUSTNESS
```

Do not start it automatically.

## GPU Runtime and Scaling Corrective Iteration

Date: 2026-08-26

Verdict remains: Phase 5 PARTIAL.

This pass prioritized moving `qwen3:8b` inference from CPU to the local NVIDIA RTX 4070 before continuing the expensive live scale gates. It did not start Phase 5B, Phase 6, public-web validation, embeddings, parallel workers, or a model bake-off.

### CPU Assignment 100 Interruption

An in-progress CPU-only assignment 100 run was stopped intentionally before GPU repair. It had reached approximately item 53 and is diagnostic only. It does not supersede completed assignment 10/25/50 evidence and is not a final assignment 100 result.

### GPU Root Cause

Ollama reported:

```text
ollama version: 0.33.0
qwen3:8b: 5.2 GB, Q4_K_M
ollama ps before repair: PROCESSOR=100% CPU, CONTEXT=8192
nvidia-smi: NVIDIA GeForce RTX 4070, driver 595.95, CUDA 13.2, 12282 MiB VRAM
```

The active Ollama installation was incomplete/mismatched. The installed tree contained CPU runner files but lacked usable CUDA backend runner files such as `ggml-cuda.dll`, and direct `llama-server.exe --list-devices` returned no devices. Active server logs showed only CPU compute discovery and `total_vram="0 B"`, while older logs had previously detected the RTX 4070. BrowserAgent was not the cause.

### Runtime Change

The official Ollama Windows installer from `https://ollama.com/download/OllamaSetup.exe` was used to repair the local Ollama installation after unloading `qwen3:8b`.

After repair, the install tree contained CUDA/Vulkan backend files, including:

```text
cuda_v12/ggml-cuda.dll
cuda_v13/ggml-cuda.dll
vulkan/ggml-vulkan.dll
```

The repaired Ollama server logs reported:

```text
library=CUDA
name=CUDA0
description="NVIDIA GeForce RTX 4070"
driver=13.2
total="12.0 GiB"
available="10.8 GiB"
```

### GPU Verification

GPU-backed Qwen inference was verified with both Ollama and NVIDIA tools:

```text
ollama ps after repair: qwen3:8b, PROCESSOR=100% GPU, CONTEXT=8192
nvidia-smi: llama-server.exe present as a compute process
VRAM during/after warm request: about 8.0 GiB used of 12.0 GiB
GPU utilization during representative request: up to 93%
```

### CPU vs GPU Representative Inference

The CPU baseline reuses the prior 50-call representative structured stress. The GPU comparison used the same BrowserAgent Ollama client/action-schema path for 20 sequential representative calls.

| Metric | CPU baseline | RTX 4070 |
| ------ | -----------: | -------: |
| Requests | 50 | 20 |
| Success rate | 50/50 | 20/20 |
| Raw endpoint failures | 0 | 0 |
| Success after retry | 50/50 | 20/20 |
| Retries | 0 | 0 |
| p50 latency | 1.18s | 3.18s |
| p95 latency | 29.23s | 3.24s |
| max latency | 50.51s | 3.69s |
| generation throughput | about 9 tok/s on long CPU generations | 71.34 tok/s average |
| max active inference requests | 1 | 1 |

The GPU path removes the CPU long-tail that caused the earlier timeout misclassification. The existing measured 60-90 second live fixture timeout remains conservative and was not reduced.

### Structured Result Guard

Research 25 initially produced one strict recall miss after GPU repair:

```text
missing key: research_012.html|education_discount
page contained fact: yes
PageObservation contained fact: yes
model endpoint failure: no
finish result: malformed inner JSON string "{"
```

The child action schema was valid because `FinishAction.result` is a string, but the batch structured result contract requires valid JSON. The orchestrator previously converted malformed finish text into an empty free-text result and committed it, silently losing the evidence-bearing field.

Corrective change:

- Structured batch contracts now reject malformed or non-object `finish.result` JSON before ledger persistence.
- The item is classified as `CONTRACT`.
- If attempts remain, the retryable item clears its stale `browser_task_id` so the next attempt starts a fresh child instead of resuming the already-completed malformed child.
- Generic unstructured contracts retain the legacy free-text fallback.

Regression coverage:

```text
test_structured_contract_retries_malformed_finish_result
```

### Assignment GPU Scaling

| Targets | Completed | Failed | Blocked | Precision | Recall | F1 | Raw Findings | Deduped Findings | Calls/Item | Duration | RSS Peak |
| ------: | --------: | -----: | ------: | --------: | -----: | -: | -----------: | ---------------: | ---------: | -------: | -------: |
| 10 | 10 | 0 | 0 | 1.00 | 1.00 | 1.00 | 7 | 4 | 2.00 | 43.09s | 64.46 MB |
| 25 | 25 | 0 | 0 | 1.00 | 1.00 | 1.00 | 15 | 4 | 2.00 | 722.23s CPU | 66.46 MB |
| 50 | 50 | 0 | 0 | 1.00 | 1.00 | 1.00 | 30 | 4 | 2.00 | 1396.69s CPU | 68.04 MB |
| 100 | 100 | 0 | 0 | 1.00 | 1.00 | 1.00 | 61 | 4 | 2.00 | 406.07s GPU | 68.35 MB |

Assignment 100 GPU details:

```text
true positives: 4
false positives: 0
false negatives: 0
model calls: 200
inference retries: 0
endpoint failures: 0
median item duration: not emitted by current fixture summary
p95 item duration: not emitted by current fixture summary
ollama ps after run: PROCESSOR=100% GPU
VRAM after run: about 8066 MiB / 12282 MiB
GPU utilization after run snapshot: 20%
```

Throughput comparison:

| Run | Processor | Targets | Seconds/Item | Items/Minute | Items/Hour |
| --- | --------- | ------: | -----------: | -----------: | ---------: |
| assignment 25 | CPU | 25 | 28.89 | 2.08 | 124.6 |
| assignment 50 | CPU | 50 | 27.93 | 2.15 | 128.9 |
| assignment 100 | GPU | 100 | 4.06 | 14.78 | 886.5 |

### Research GPU Scaling

| Targets | Completed | Failed | Blocked | Field Precision | Field Recall | F1 | Raw Findings | Deduped Findings | Calls/Item | Duration | RSS Peak |
| ------: | --------: | -----: | ------: | --------------: | -----------: | -: | -----------: | ---------------: | ---------: | -------: | -------: |
| 10 | 10 | 0 | 0 | 1.00 | 1.00 | 1.00 | 6 | 3 | 2.10 | 246.29s CPU | 64.00 MB |
| 25 | 25 | 0 | 0 | 1.00 | 1.00 | 1.00 | 15 | 3 | 2.04 | 99.69s GPU | 66.46 MB |
| 50 | 50 | 0 | 0 | 1.00 | 1.00 | 1.00 | 30 | 3 | 2.08 | 196.17s GPU | 67.98 MB |

Research 50 field details:

```text
truth_count: 30
predicted_count: 30
true positives: 30
false positives: 0
false negatives: 0
pricing: precision 1.00, recall 1.00, found 10
education_discount: precision 1.00, recall 1.00, found 10
public_api_docs: precision 1.00, recall 1.00, found 10
unsupported findings: 0
```

### Prompt and RSS Scaling

Assignment 100 GPU prompt checkpoints:

```text
item 1: 1059
item 10: 1059
item 25: 1059
item 50: 1059
item 100: 1059
average: 1118.29
p95: 1242
max: 1242
```

Research 50 GPU prompt checkpoints:

```text
item 1: 1071
item 10: 1071
item 25: 1071
item 50: 1071
average: 1103.69
p95: 1130
max: 1221
```

RSS checkpoints:

| Run | Startup | Item 10 | Item 25 | Item 50 | Item 100 | Peak |
| --- | ------: | ------: | ------: | ------: | -------: | ---: |
| assignment 100 GPU | 51.06 MB | 64.64 MB | 66.68 MB | 67.86 MB | 68.35 MB | 68.35 MB |
| research 50 GPU | 51.06 MB | 64.77 MB | 66.84 MB | 67.98 MB | n/a | 67.98 MB |

Prompt and RSS measurements show no batch-ordinal context growth through assignment 100 or research 50.

### Tests

Focused orchestrator tests:

```text
9 passed
```

Full deterministic suite:

```text
134 passed in 76.85s
```

### Remaining Validation State

The local extraction scale gates now pass:

- assignment 10/25/50/100
- research 10/25/50

The Phase 5 verdict remains PARTIAL because these PASS gates are still not complete:

- real process-kill batch crash/resume matrix at 25%/50%/75%
- failure-mix benchmark
- representative Phase 4B regression
- public-web pilot after local gates

Model decision:

```text
QWEN3-8B SUFFICIENT for completed local assignment/research extraction gates.
```

Embedding decision:

```text
FTS5 SUFFICIENT
```

Parallelism decision:

```text
SEQUENTIAL SUFFICIENT for Phase 5 local scale gates. Controlled parallelism is not justified before Phase 5 completion.
```

Next step:

```text
Continue existing Phase 5 validation only: implement/run real crash-resume matrix, failure mix, Phase 4B regression, then public-web pilot last.
```

## Inference Reliability Corrective Iteration

Date: 2026-08-26

Verdict remains: Phase 5 PARTIAL.

This corrective pass did not start Phase 5B, Phase 6, public-web validation, embeddings, parallel workers, or a model bake-off.

### Root Cause

The prior research 10 `precision=0` result was not a model-quality verdict. The six relevant research pages failed before ledger result creation because Ollama inference exceeded the old effective timeout envelope while running `qwen3:8b` on CPU.

Runtime checks showed:

```text
ollama ps: qwen3:8b resident after warmup, PROCESSOR=100% CPU, CONTEXT=8192
nvidia-smi: RTX 4070 present, about 1.9 GB VRAM in use, no Ollama compute process using VRAM
active inference requests: max 1
```

The desktop has an NVIDIA GPU available, but this Ollama runtime path used CPU for `qwen3:8b`. Long structured finish generations on CPU consumed the full `num_predict=256` budget and took about 28-50s, so the old 30s scalar timeout was too close to the p95/p99 tail.

### Runtime Diagnostics

Raw trivial Ollama API probe through `/api/generate`:

```text
requests: 20
successes: 20
failures: 0
p50: 620.7 ms
p95: 651.4 ms
max: 7567.7 ms
```

Representative BrowserAgent research step-2 prompt:

```text
prompt chars: 4971
estimated tokens: 1280
Ollama prompt tokens: 1175
schema: production action JSON schema
num_ctx: 8192
num_predict: 256
```

Representative 50-call structured stress:

```text
requests: 50
raw successes: 50
raw failures: 0
success after retry: 50
failures after retry: 0
p50: 1184.31 ms
p95: 29230.63 ms
max: 50506.43 ms
max active requests: 1
endpoint failure taxonomy: none
```

Controlled structured-output probes:

| Case | Requests | Successes | Failures | p50 | p95 | Max |
| ---- | -------: | --------: | -------: | --: | --: | --: |
| Current action schema | 10 | 10 | 0 | 1139.01 ms | 28816.06 ms | 28816.06 ms |
| Small synthetic schema | 10 | 10 | 0 | 28852.05 ms | 29276.93 ms | 29276.93 ms |
| Plain JSON | 10 | 10 | 0 | 1201.33 ms | 24677.06 ms | 24677.06 ms |

Conclusion: the observed failures correlate with CPU-bound long generation and timeout margin, not general Ollama endpoint death and not uniquely with `oneOf` schema complexity. Removing structured output is not justified by this evidence.

### Corrective Changes

Inference client changes:

- Added explicit inference failure taxonomy: `CONNECT_TIMEOUT`, `READ_TIMEOUT`, `TOTAL_REQUEST_TIMEOUT`, `OLLAMA_HTTP_ERROR`, `CONNECTION_RESET`, `SERVICE_UNAVAILABLE`, `MALFORMED_RESPONSE`, `STRUCTURED_OUTPUT_FAILURE`, `UNKNOWN_INFERENCE_FAILURE`.
- Replaced the Ollama scalar timeout with explicit `connect`, `read`, `write`, and `pool` timeout settings.
- Added bounded inference-level retry for transient endpoint categories only.
- Added configurable Ollama `keep_alive`, default `5m`.
- Added request diagnostics: request id, attempt, model, prompt hash/chars/tokens when available, schema type, response-header timing, total duration, prompt/eval durations, done reason, HTTP status, exception class/message, retryability, and active request count.

Agent/batch wiring:

- `AgentLoop` now writes `inference_request` metrics for successful and failed attempts.
- Batch runtime policy carries `batch_id` and `work_item_id` into child task metrics.
- Batch child failure classification now maps inference timeout/service categories instead of leaving them as generic `UNKNOWN`.
- Research ledger ingress now preserves valid findings when `fields[field]` is the string `"found"` and the matching finding carries value/evidence.

The research ledger fix was independently proven after stable inference: live child completions contained canonical findings, but normalization dropped them because the string `found` field placeholder preempted the later evidence-bearing finding.

### Research 10 Rerun

Exact local research 10 fixture rerun after inference hardening and ledger-ingress fix:

```text
completed: 10
failed: 0
blocked: 0
raw findings: 6
deduplicated findings: 3
field precision: 1.00
field recall: 1.00
found: 6
not_found: 12
unsupported findings: 0
inference attempts: 21
inference retries: 0
endpoint failures: 0
duration: 246.29s
RSS startup: 50.90 MB
RSS item 10: 64.00 MB
RSS peak: 64.00 MB
```

### Assignment 10 Regression

Assignment 10 after the inference changes:

```text
completed: 10
failed: 0
blocked: 0
precision: 1.00
recall: 1.00
raw findings: 7
deduplicated findings: 4
inference attempts: 20
inference retries: 0
endpoint failures: 0
duration: 239.83s
RSS startup: 51.04 MB
RSS item 10: 63.70 MB
RSS peak: 63.70 MB
```

### Tests

Focused reliability/regression slice:

```text
21 passed
```

Full deterministic suite:

```text
133 passed in 90.54s
```

Added coverage for:

- timeout classification
- retryable inference error
- non-retryable inference error
- bounded retry
- client cleanup after timeout
- structured response after retry
- repeated timeout cleanup
- no duplicate action intent after inference retry
- research `fields[field] = "found"` plus matching finding evidence

Added a live model-marked Ollama reliability diagnostic for repeated sequential structured calls. It remains under `pytest -m model` and is not part of ordinary deterministic CI.

### Remaining Validation State

The Phase 5 verdict remains PARTIAL because the remaining Phase 5 PASS gates have not been completed in this corrective pass:

- assignment 25/50/100 after corrected contracts
- research 25/50
- RSS checkpoints at 25/50/100
- real process-kill crash/resume matrix
- failure-mix benchmark
- representative Phase 4B regression
- public-web pilot

Model decision:

```text
QWEN3-8B SUFFICIENT for local assignment 10 and research 10 gates; INSUFFICIENT EVIDENCE for full Phase 5 scaling.
```

Embedding decision:

```text
FTS5 SUFFICIENT
```

Parallelism decision:

```text
SEQUENTIAL SUFFICIENT for current corrective gates; controlled parallelism is not justified while CPU-bound inference and remaining validation are unresolved.
```

Next step:

```text
NEEDS ANOTHER PHASE 5 CORRECTIVE ITERATION
```
