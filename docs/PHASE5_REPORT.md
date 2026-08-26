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

