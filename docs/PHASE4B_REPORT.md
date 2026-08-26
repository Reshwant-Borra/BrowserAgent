# Phase 4B Report: Memory Application and Completion Reliability

Date: 2026-08-25

Branch tested: `main` at `602d61d`, with local Phase 4B follow-up changes.

## Verdict

Phase 4B PASS.

The final evidence meets the requested gate:

- Deterministic tests: 98/98 passing.
- Short smoke: 15/15 passing.
- Short holdout: 5/5 passing.
- Primary long-horizon E replication: 15/15 passing.
- Long holdout E replication: 9/9 passing.
- Crash/resume matrix: 9/9 passing.
- Required observed facts persisted: 54/54 across final primary + holdout E runs.
- Required facts present in context when needed: 54/54 across final primary + holdout E runs.
- Required facts applied: 54/54 across final primary + holdout E runs.
- Prompt budget: max observed prompt tokens 1406, below the 4096 normal-step budget.

## Baseline

The pre-Phase 4B failure mode was not token overflow. Required facts were usually observed and present in the prompt, but Qwen did not reliably apply them at final controls or downloaded the wrong visible artifact early.

The precise baseline classification was `PRESENT_NOT_APPLIED`, not `STORAGE_MISS`, `RETRIEVAL_MISS`, or `PROMPT_MISS`.

## Root-Cause Pipeline

Final evidence supports this pipeline:

1. Facts were observed on early workflow pages.
2. Facts were persisted into `active_task_facts` and `task_memories`.
3. Page-aware retrieval and active facts brought them back into context.
4. The remaining failure was applying high-confidence facts to mapped controls or blocking premature contradictory actions.

The fix is deterministic assistance at the application boundary, not embeddings, page deltas, or a larger planner.

## Changes

Implemented before this report:

- Page-aware retrieval query construction.
- Incremental derived-memory ingestion via `task_memory_ingest_state`.
- Structured active facts with provenance.
- Generic fact extraction for requirements, facts, key/value lines, and artifact targets.
- Fixed-section running summary rebuilt from canonical events.
- Memory pipeline metrics and memory-action matching metrics.
- Non-leaky `completion_criteria`, separate hidden `evaluation_criteria`, and hidden `memory_expectations`.
- Optional strict active-fact constraint guard.
- Guard corrections for mapped config controls, ordered research facts, and wrong-artifact downloads.

Additional fix during desktop validation:

- `agent/loop.py` no longer treats successful repeated semantic actions as loops. Repeated `Continue` clicks that advance the URL/page hash are normal in long workflows; before this fix, they caused unnecessary recovery/replan churn and Tier 5 max-step exhaustion.

## Ablations

Post-fix one-trial ablation over 5 long tasks:

| Variant | Passes | Prompt avg | Prompt max | Retrieved total |
| --- | ---: | ---: | ---: | ---: |
| A recent only | 2/5 | 937.4 | 1137 | 0 |
| B summary | 3/5 | 1015.4 | 1607 | 0 |
| C page-aware retrieval | 3/5 | 1107.8 | 1760 | 288 |
| D active facts | 1/5 | 1264.4 | 1846 | 2004 |
| E constraint guard | 5/5 | 1233.3 | 1351 | 1584 |

E is the only variant that passed all represented config/research/download Tier 4 and Tier 5 tasks in the controlled ablation.

## Long-Horizon Results

Primary E replication, 3 trials each:

| Task | Passes | Actions avg | Calls avg | Recoveries |
| --- | ---: | ---: | ---: | ---: |
| `tier4_config_36` | 3/3 | 39 | 39 | 0 |
| `tier4_research_32` | 3/3 | 34 | 35 | 3 |
| `tier4_download_42` | 3/3 | 43 | 43 | 0 |
| `tier5_config_96` | 3/3 | 100 | 100 | 0 |
| `tier5_download_105` | 3/3 | 106 | 106 | 0 |

Aggregate: 15/15 passing, prompt avg 1235.8, prompt max 1354.

## Holdout Results

Long holdout E replication, 3 trials each:

| Task | Passes | Actions avg | Calls avg | Recoveries |
| --- | ---: | ---: | ---: | ---: |
| `holdout_config_balanced_44` | 3/3 | 48 | 49.3 | 8 |
| `holdout_research_38` | 3/3 | 40 | 40 | 0 |
| `holdout_download_88` | 3/3 | 89 | 97.3 | 0 |

Aggregate: 9/9 passing, prompt avg 1257.8, prompt max 1406.

Short regressions:

- Smoke 3x: 15/15 passing.
- Holdout 1x: 5/5 passing.

## Context Results

Normal prompts remained comfortably bounded:

- Primary E max prompt tokens: 1354.
- Holdout E max prompt tokens: 1406.
- Crash/resume matrix max prompt tokens: 1192.

No evidence supports page deltas as necessary for this fixture class.

## Retrieval Metrics

Final E runs showed retrieval active without budget pressure:

- Primary E replication: 4752 retrieved-memory selections total.
- Holdout E replication: 2615 retrieved-memory selections total.
- Crash/resume matrix: retrieval calls present in every case, from 49 to 154 calls depending on workflow length and kill point.

No final failure was classified as `RETRIEVAL_MISS`.

## Active Fact Metrics

Primary E replication:

- Required facts observed: 33/33.
- Required facts persisted: 33/33.
- Required facts selected/prompted: 33/33.
- Required facts applied: 33/33.

Holdout E replication:

- Required facts observed: 21/21.
- Required facts persisted: 21/21.
- Required facts selected/prompted: 21/21.
- Required facts applied: 21/21.

Combined final E evidence: 54/54 at every required-fact stage.

## Memory Application Metrics

The E guard converted prompt-present facts into deterministic corrections:

- Config tasks: mapped select/textbox controls were filled from active facts before save.
- Research tasks: `fact 1`, `fact 2`, `fact 3` were applied as an ordered combined string.
- Download tasks: wrong artifact download attempts were corrected by advancing with `Continue`/`Next` until the required artifact was the visible download target.

The remaining non-E variants still show that retrieval and prompting alone are not sufficient.

## Completion Behavior

The benchmark keeps `model_finish`, `environment_success`, and hidden evaluation success separate. Final E primary and holdout runs had both environment success and model finish for every task.

Download auto-completion after verified download worked in primary Tier 4/Tier 5 runs. Holdout download required contract repairs in some trials but still completed correctly.

## Loop Behavior

The desktop run found and fixed one loop-detection bug: repeated successful `Continue` actions were being escalated as loops. After gating repeated-action loop escalation on failure/no-op, recovery churn dropped sharply and Tier 5 completed within budget.

This is not a Phase 5 change; it is a Phase 4B correctness fix for long linear workflows.

## Crash/Resume

Crash/resume matrix used real subprocess termination and separate resume processes:

| Case | Kill Points | Passes |
| --- | --- | ---: |
| `config_beta_44` | 25%, 50%, 75% | 3/3 |
| `research_beta_38` | 25%, 50%, 75% | 3/3 |
| `download_beta_88` | 25%, 50%, 75% | 3/3 |

Verified in every case:

- Final state completed.
- Event replay state completed.
- Pending action intent cleared.
- Active facts rebuilt.
- Summary present.
- Retrieval remained active after resume.
- No duplicate consequential action.

## Performance

Observed latencies were stable across long runs:

- Primary E: early avg 528.8 ms, late avg 510.7 ms.
- Holdout E: early avg 519.6 ms, late avg 500.8 ms.
- Post-fix ablation E: early avg 520.0 ms, late avg 508.3 ms.

The memory pipeline overhead stayed small relative to model latency, and prompt sizes did not grow with task length.

## Decisions

Embedding decision: FTS5 SUFFICIENT

Page delta decision: NOT NEEDED

Model decision: QWEN3-8B SUFFICIENT

Rationale: final failures were not retrieval misses. The E guard passed primary and holdout long-horizon tasks with Qwen3-8B/Q4_K_M under the normal prompt budget. Embeddings, page deltas, and a model bake-off are not justified by the Phase 4B evidence.

## Remaining Failures

No final E primary, holdout, short smoke, short holdout, deterministic, or crash/resume failures remain.

Residual risk:

- The crash/resume matrix uses deterministic prompt-aware decisions rather than live Qwen decisions to isolate infrastructure behavior.
- The tested sites are local fixtures, not arbitrary production websites.
- Holdout download still needed contract repair calls, so output-contract reliability should remain monitored.
