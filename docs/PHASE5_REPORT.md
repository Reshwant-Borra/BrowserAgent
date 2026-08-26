# Phase 5 Report: Multi-Site Orchestration, Persistent Work Queues, and Result Ledger

Date: 2026-08-26

Branch: `phase5-multisite-orchestration`

Starting baseline: `6416888 fix: complete phase4b validation`

## Verdict

Phase 5 PARTIAL.

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

This is not a Phase 5 PASS because live Qwen 10/25/50/100 multisite extraction, batch crash/resume process termination, failure-mix benchmarks, and public-web pilot have not been completed.

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
