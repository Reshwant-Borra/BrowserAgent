# Phase 4 Report

## Verdict

PARTIAL.

Phase 4 implemented bounded tiered context, deterministic summary compaction, task-local
FTS memory, token accounting by prompt block, long-horizon fixtures, ablation runners, and
generic completion guards. Short-task Phase 1B behavior remains green, and prompt context
stays bounded in corrected long runs. The current system does not yet meet the long-horizon
success target because hidden-value configuration and research workflows still fail.

## Architecture Implemented

- Static prefix: existing system/action contract.
- Semi-stable task state: goal, success criteria, current subgoal, plan.
- Running summary: event-derived compact summary of older history.
- Recent raw window: bounded `recent_actions` tail.
- Retrieved task memory: SQLite FTS5 over task-local derived memories.
- Current page: unchanged compact `PageObservation`; page deltas were not implemented.

The invariant remains:

```text
events = truth
task_state = derived
summary = derived
task memories = derived
context = temporary working set
```

## Key Implementation Details

- Added context budgets in `config/default.yaml`:
  `max_total_tokens=4096`, `recent_window_tokens=800`, `summary_tokens=500`,
  `retrieved_memory_tokens=500`, `page_tokens=1400`.
- Added per-model-call metrics:
  `context_block_tokens`, `context_block_chars`, `total_estimated_prompt_tokens`,
  `retrieved_memory_count`, `running_summary_tokens`.
- Added `task_summaries`, `task_memories`, and `task_memories_fts` tables.
- Added compaction events:
  `COMPACTION_STARTED`, `SUMMARY_CREATED`, `COMPACTION_COMMITTED`.
- Added deterministic memory writing for verified downloads/extractions, failed paths,
  completed steps, blockers, subgoal decisions, and salient observation text.
- Added a generic completion guard:
  verified download plus satisfied criteria can complete without repeated downloads.
- Added grounded finish validation:
  failed model `finish` claims are not treated as evidence.
- Added stale-target validation for `extract(target)`.

## Short-Task Regression

Final verified run:

```text
Smoke: 15 / 15
Holdout: 5 / 5
Tests: 88 passed
```

Run directories:

```text
runtime/benchmark_runs/phase4_short_regression_verified
runtime/benchmark_runs/phase4_short_holdout_verified
```

## Corrected Long-Horizon Evidence

Important correction: the first long benchmark version leaked exact answers through
`success_criteria`. The task YAML now separates prompt-facing `success_criteria` from
hidden `evaluation_criteria`.

Corrected partial ablation results:

| Variant | Task | Actions | Calls | Pass | Main failure | Max prompt tokens |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| A recent only | tier4_config_36 | 43 | 43 | 0 | VERIFICATION | 1023 |
| A recent only | tier4_research_32 | 3 | 4 | 0 | COMPLETION | 846 |
| A recent only | tier4_download_42 | 4 | 5 | 1 |  | 857 |
| A recent only | tier5_config_96 | 99 | 99 | 0 | VERIFICATION | 999 |
| A recent only | tier5_download_105 | 4 | 5 | 1 |  | 861 |
| B summary | tier4_config_36 | 43 | 43 | 0 | VERIFICATION | 1613 |
| B summary | tier4_research_32 | 3 | 4 | 0 | COMPLETION | 846 |
| B summary | tier4_download_42 | 4 | 5 | 1 |  | 857 |
| C summary+retrieval | tier4_config_36 | 43 | 43 | 0 | VERIFICATION | 1775 |
| C summary+retrieval | tier4_download_42 | 5 | 16 | 1 |  | 1324 |
| C summary+retrieval | tier4_research_32 | 2 | 3 | 0 | COMPLETION | 898 |

The clean C config rerun persisted the required early facts (`Advanced`, `North`, `AX-47`)
with source-event provenance, but Qwen did not apply them successfully before the step cap.

## Context Growth

Corrected long runs stayed bounded:

| Variant/task | Step 10 | Step 25 | Step 50 | Step 100+ |
| --- | ---: | ---: | ---: | ---: |
| A tier5_config_96 | 908 | 927 | 929 | 916 |
| B tier4_config_36 | 1271 | 1522 | 1595 | n/a |
| C tier4_config_36 clean | ~bounded | ~bounded | n/a | n/a |

No observed corrected run approached the 4096-token ceiling.

## Average Context Breakdown

Representative corrected runs:

| Run | Static | Task | Summary | Recent | Memory | Page |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A tier5_config_96 | 719 | 97 | 0 | 55 | 0 | 90 |
| B tier4_config_36 | 719 | 93 | 445 | 53 | 0 | 95 |
| C tier4_config_36 clean | 719 | n/a | active | active | active | n/a |

Page observation is not the dominant token consumer in these fixtures.

## Latency

Representative corrected runs:

| Run | Early avg ms | Late avg ms |
| --- | ---: | ---: |
| A tier5_config_96 | 1685 | 1751 |
| B tier4_config_36 | 3177 | 4148 |
| C tier4_config_36 clean | 3952 | 5840 |

Latency remained bounded for A, but summary/retrieval variants increased late-step latency.

## Crash/Resume

Existing Phase 3 crash recovery remains green. Phase 4 added reconstruction tests around
summary events and derived memory rebuilds. The full requested 25%/50%/75% long-task
kill/resume matrix was not completed in this pass, so Phase 4 cannot be marked PASS.

## Page Delta Decision

NOT NEEDED for the next step based on current evidence. Page tokens were usually tens to
low hundreds; static prefix and summary/retrieval dominate more than page observation in
these runs.

## Embedding Decision

INSUFFICIENT EVIDENCE. FTS5 found stored memories in the failing C config run, and the
required facts existed in SQLite with provenance. The observed failure is not yet clearly a
lexical retrieval miss. Improve memory presentation/usefulness measurement before adding
embeddings.

## Remaining Failure Modes

1. Retrieved facts are not reliably applied on hidden-value configuration workflows.
2. Research workflows with no prompt-facing criteria can finish too early.
3. Long config tasks can consume many calls while remaining bounded in context.
4. Full long-task crash/resume matrix is missing.
5. Retrieval metrics count hits but do not yet label useful versus irrelevant hits.

## Next Recommended Phase

Phase 4B should stay on the same model and fix memory usefulness before adding embeddings:

- Add explicit retrieved-memory usefulness instrumentation.
- Make prompt-facing success criteria non-leaky but still completion-oriented.
- Improve deterministic summary format for requirements/facts separate from routine progress.
- Add long-task crash/resume at 25%/50%/75%.
- Re-run corrected A/B/C plus holdouts with at least one full clean trial per task.
