# Phase 4B RTX 4070 Super Handoff

Date: 2026-08-25

Branch: `phase4b-memory-application`

Starting project commit: `a1a8201 feat: add bounded long-horizon context memory`

## What Was Implemented

This branch contains Phase 4B work focused on memory application and completion reliability, not Phase 5.

Implemented:

- Page-aware retrieval query construction using current page controls, labels, options, subgoal, active fact keys, goal, completion criteria, and page title/domain.
- Incremental derived-memory ingestion using `task_memory_ingest_state` plus `EventStore.events_after()`.
- Provenance-backed structured active facts in `active_task_facts`.
- Generic fact extraction for patterns such as `Use <key> <value>`, `Remember <key> <value>`, `The required <thing> is <value>`, `Fact N: <value>`, and `<key> = <value>`.
- Fixed-section semantic running summary rebuilt from canonical events without nested “Previous summary” drift.
- Reduced cognitive-memory pollution from routine successful clicks.
- Active fact prompt block with explicit mappings like `[1] Mode <- mode = Advanced`.
- Ordered collected-facts bundle for research tasks: `collected facts in order = ...`.
- Per-call memory pipeline metrics:
  - retrieval query terms
  - candidate memory IDs
  - selected memory IDs/kinds/source event IDs
  - selected memory tokens
  - active fact IDs/source event IDs/tokens
  - context construction timings
- Memory-action value matching metrics.
- Semantic action signatures for wrong-value loop detection.
- Non-leaky benchmark `completion_criteria`, separate from hidden `evaluation_criteria`.
- Hidden evaluator-only `memory_expectations`.
- Optional strict deterministic constraint guard behind `context.enforce_active_fact_constraints`.
- Guard corrections for deterministic mapped controls:
  - fill/select visible controls from high-confidence active facts
  - correct combined-facts textboxes to the ordered collected-facts string
  - avoid wrong artifact downloads and click `Continue`/`Next` when the required artifact is not the visible download target yet

## What Was Verified On Laptop

Deterministic tests:

```bash
.venv/bin/python -m pytest -q
# 98 passed in 15.80s

.venv/bin/python -m pytest tests/unit tests/integration -q
# 97 passed in 10.48s
```

Short live Qwen/Ollama regression:

```bash
.venv/bin/python benchmarks/run_smoke_benchmarks.py \
  --tasks-file benchmarks/smoke_tasks.yaml \
  --trials 3 \
  --output-dir runtime/benchmark_runs/phase4b_short_smoke_3x
# 15/15 passed

.venv/bin/python benchmarks/run_smoke_benchmarks.py \
  --tasks-file benchmarks/holdout_tasks.yaml \
  --trials 1 \
  --output-dir runtime/benchmark_runs/phase4b_short_holdout
# 5/5 passed
```

Latest long live Qwen/Ollama partial run:

```bash
.venv/bin/python benchmarks/run_long_horizon_benchmarks.py \
  --tasks-file benchmarks/long_horizon_tasks.yaml \
  --trials 1 \
  --variant E_constraint_guard \
  --output-dir runtime/benchmark_runs/phase4b_e5_constraint_guard_v4
```

Saved Tier 4 results from that run:

| Task | Result | Actions | Calls | Pipeline |
| ---- | ------ | ------: | ----: | -------- |
| `tier4_config_36` | PASS | 39 | 40 | 3 observed, 3 persisted, 3 selected, 3 prompted, 3 applied |
| `tier4_research_32` | PASS | 34 | 36 | 3 observed, 3 persisted, 3 selected, 3 prompted, 3 applied |
| `tier4_download_42` | PASS | 43 | 48 | 1 observed, 1 persisted, 1 selected, 1 prompted, 1 applied |

The laptop run was intentionally interrupted before Tier 5 because it was too demanding.

## Important Findings So Far

The original Phase 4B diagnosis was correct: this was not primarily token overflow and not clearly retrieval recall.

Observed failure progression:

- D active facts without strict guard:
  - Required facts were observed, persisted, selected, and present in prompt.
  - Qwen repeatedly clicked `Save configuration` without applying all values.
  - Classification: `PRESENT_NOT_APPLIED`.

- E strict guard before correction:
  - Guard detected unresolved mapped facts before save.
  - Qwen still retried bad submit.
  - Classification remained `PRESENT_NOT_APPLIED`.

- E strict guard with deterministic corrections:
  - Config passed by correcting missing `select`/`type` actions.
  - Research passed after adding ordered collected-facts handling.
  - Download passed after adding wrong-artifact guard that advances via `Continue`/`Next` instead of downloading a mismatched artifact.

This supports the architectural principle:

> Once facts are observed, verified, and provenance-backed, deterministic infrastructure should make them easy to apply and should prevent contradictory or premature actions when the mapping is high confidence.

## What Remains To Run On RTX 4070 Super Desktop

Do not claim Phase 4B PASS until these are run and documented:

1. Full deterministic tests from clean checkout.
2. Short live regression:
   - smoke 3x
   - holdout 1x or 3x if time allows
3. Final long ablation:
   - A recent only
   - B summary
   - C page-aware retrieval
   - D active facts
   - E constraint guard
4. Final primary long-horizon replication:
   - at least 3 Tier 4 trials
   - at least 2 clean Tier 5 trials if runtime permits
5. Long holdout:
   - beta config
   - beta research
   - beta download
6. Crash/resume matrix:
   - config, research, download
   - kill at about 25%, 50%, 75%
   - verify replay, task state, active facts, summary, retrieval, no unsafe duplicate action, and final success
7. Completion evaluator audit:
   - keep `model_finish`, `environment_success`, and hidden evaluation success separate
8. Final `docs/PHASE4B_REPORT.md`.
9. Decide:
   - Embedding decision: `FTS5 SUFFICIENT`, `EMBEDDINGS JUSTIFIED`, or `INSUFFICIENT EVIDENCE`
   - Page delta decision: expected `NOT NEEDED`
   - Model decision: likely depends on E-guard matrix and holdout results

## Suggested Desktop Commands

Use the project root:

```bash
cd /path/to/BrowserAgent
git checkout phase4b-memory-application
git pull origin phase4b-memory-application
```

Verify Ollama and model:

```bash
ollama list
ollama run qwen3:8b "ok"
```

Run deterministic tests:

```bash
.venv/bin/python -m pytest -q
```

Run short live regression:

```bash
.venv/bin/python benchmarks/run_smoke_benchmarks.py \
  --tasks-file benchmarks/smoke_tasks.yaml \
  --trials 3 \
  --output-dir runtime/benchmark_runs/phase4b_desktop_short_smoke_3x

.venv/bin/python benchmarks/run_smoke_benchmarks.py \
  --tasks-file benchmarks/holdout_tasks.yaml \
  --trials 1 \
  --output-dir runtime/benchmark_runs/phase4b_desktop_short_holdout
```

Run final ablation one trial first:

```bash
.venv/bin/python benchmarks/run_long_horizon_benchmarks.py \
  --tasks-file benchmarks/long_horizon_tasks.yaml \
  --trials 1 \
  --variant all \
  --output-dir runtime/benchmark_runs/phase4b_desktop_final_ablation_1x
```

Run E replication:

```bash
.venv/bin/python benchmarks/run_long_horizon_benchmarks.py \
  --tasks-file benchmarks/long_horizon_tasks.yaml \
  --trials 3 \
  --variant E_constraint_guard \
  --output-dir runtime/benchmark_runs/phase4b_desktop_e_guard_3x
```

Run holdout:

```bash
.venv/bin/python benchmarks/run_long_horizon_benchmarks.py \
  --tasks-file benchmarks/long_horizon_holdout_tasks.yaml \
  --trials 3 \
  --variant E_constraint_guard \
  --output-dir runtime/benchmark_runs/phase4b_desktop_holdout_e_guard_3x
```

If Tier 5 runtime is too high, run at least two E trials and clearly label incomplete replication:

```bash
.venv/bin/python benchmarks/run_long_horizon_benchmarks.py \
  --tasks-file benchmarks/long_horizon_tasks.yaml \
  --trials 2 \
  --variant E_constraint_guard \
  --output-dir runtime/benchmark_runs/phase4b_desktop_e_guard_2x
```

## Continuation Prompt For RTX 4070 Super Desktop

Copy the following prompt into Codex on the desktop after pulling this branch:

```text
You are continuing BrowserAgent Phase 4B on my desktop with an RTX 4070 Super.

Repository:
https://github.com/Reshwant-Borra/BrowserAgent.git

Branch:
phase4b-memory-application

Current Phase 4B handoff:
Read docs/PHASE4B_RTX4070_HANDOFF.md first.

Important: this is still Phase 4B, not Phase 5. Do not implement embeddings, page deltas, a second model, or a large planner unless the evidence from the requested experiments justifies the recommendation. Do not change the primary model for main experiments:

- qwen3:8b
- Ollama
- Q4_K_M

Start by confirming:

git status
git branch -vv
git log --oneline --decorate -10
git remote -v

Then read the current implementation in:

ARCHITECTURE.md
agent/loop.py
agent/context_builder.py
agent/config.py
agent/verifier.py
agent/recovery.py
agent/loop_detector.py
agent/schemas.py
agent/token_budget.py
inference/prompt.py
inference/llama_client.py
inference/grammar/action.gbnf
memory/event_store.py
memory/task_state.py
memory/replay.py
memory/task_memory.py
memory/schema.sql
memory/models.py
benchmarks/run_smoke_benchmarks.py
benchmarks/run_long_horizon_benchmarks.py
benchmarks/long_horizon_tasks.yaml
benchmarks/long_horizon_holdout_tasks.yaml
tests/unit/test_phase4_context_memory.py
tests/integration/test_phase4_long_horizon.py
tests/integration/test_phase3_crash_recovery.py
docs/PHASE1B_REPORT.md
docs/PHASE4_REPORT.md
docs/PHASE4B_RTX4070_HANDOFF.md

Current branch already implements:

- page-aware FTS query builder
- incremental derived-memory ingestion
- structured active facts
- fixed semantic summary
- memory pipeline metrics
- non-leaky completion_criteria separate from evaluation_criteria
- memory-action value matching
- semantic action signatures
- optional strict deterministic constraint guard
- guard corrections for mapped config controls, ordered collected research facts, and wrong-artifact download attempts

Known latest laptop evidence:

- Full deterministic tests: 98 passed
- Unit + integration subset after latest guard changes: 97 passed
- Short smoke 3x: 15/15 passed
- Short holdout 1x: 5/5 passed
- E_constraint_guard v4 Tier 4 one-trial primary:
  - tier4_config_36 PASS, 39 actions, 40 calls, 3/3 facts observed/persisted/selected/prompted/applied
  - tier4_research_32 PASS, 34 actions, 36 calls, 3/3 facts observed/persisted/selected/prompted/applied
  - tier4_download_42 PASS, 43 actions, 48 calls, 1/1 fact observed/persisted/selected/prompted/applied
- The laptop run was interrupted before Tier 5 because runtime was too demanding.

Your job:

1. Reproduce deterministic tests:
   .venv/bin/python -m pytest -q

2. Re-run short live regression:
   .venv/bin/python benchmarks/run_smoke_benchmarks.py --tasks-file benchmarks/smoke_tasks.yaml --trials 3 --output-dir runtime/benchmark_runs/phase4b_desktop_short_smoke_3x
   .venv/bin/python benchmarks/run_smoke_benchmarks.py --tasks-file benchmarks/holdout_tasks.yaml --trials 1 --output-dir runtime/benchmark_runs/phase4b_desktop_short_holdout

3. Run full controlled long ablation:
   .venv/bin/python benchmarks/run_long_horizon_benchmarks.py --tasks-file benchmarks/long_horizon_tasks.yaml --trials 1 --variant all --output-dir runtime/benchmark_runs/phase4b_desktop_final_ablation_1x

   Compare:
   A_recent_only
   B_summary
   C_page_aware_retrieval
   D_active_facts
   E_constraint_guard

4. Run final E replication:
   .venv/bin/python benchmarks/run_long_horizon_benchmarks.py --tasks-file benchmarks/long_horizon_tasks.yaml --trials 3 --variant E_constraint_guard --output-dir runtime/benchmark_runs/phase4b_desktop_e_guard_3x

   If Tier 5 is too slow, run at least 2 clean Tier 5-inclusive trials and label incomplete replication honestly.

5. Run holdout:
   .venv/bin/python benchmarks/run_long_horizon_benchmarks.py --tasks-file benchmarks/long_horizon_holdout_tasks.yaml --trials 3 --variant E_constraint_guard --output-dir runtime/benchmark_runs/phase4b_desktop_holdout_e_guard_3x

6. Finish crash/resume matrix:
   - config, research, download
   - kill around 25%, 50%, 75%
   - use real subprocess/process termination following tests/integration/test_phase3_crash_recovery.py
   - verify replay, task_state, active facts rebuilt, summary valid, current browser position reconciled, no unsafe duplicate action, retrieval still returns early facts, and final success

7. Analyze saved JSON artifacts and metrics:
   For each task report:
   - observed
   - persisted
   - retrieved when needed
   - in prompt
   - applied
   - verified
   - completed

   Keep classifications precise:
   STORAGE_MISS
   RETRIEVAL_MISS
   PROMPT_MISS
   PRESENT_NOT_APPLIED
   WRONG_MAPPING
   COMPLETION_EARLY
   VERIFIER_FALSE_NEGATIVE
   RECOVERY_LOOP

8. Create docs/PHASE4B_REPORT.md with:
   - Baseline
   - Root-cause pipeline
   - Changes
   - Ablations
   - Long-horizon results
   - Holdout results
   - Context results
   - Retrieval metrics
   - Active fact metrics
   - Memory application metrics
   - Completion behavior
   - Loop behavior
   - Crash/resume
   - Performance
   - Embedding decision
   - Page delta decision
   - Remaining failures
   - Verdict

9. Make evidence-based decisions:
   Embedding decision must be exactly one of:
   - FTS5 SUFFICIENT
   - EMBEDDINGS JUSTIFIED
   - INSUFFICIENT EVIDENCE

   Page delta decision must be exactly one of:
   - NEEDED
   - NOT NEEDED
   - INSUFFICIENT EVIDENCE

   Model decision must be exactly one of:
   - QWEN3-8B SUFFICIENT
   - MODEL BAKE-OFF JUSTIFIED
   - INSUFFICIENT EVIDENCE

10. Do not claim PASS unless the Phase 4B criteria are met:
    - short smoke 15/15
    - short holdout 5/5
    - >=80% long-horizon success with config/research/download represented
    - >=80% holdout success
    - required observed facts >=95% persisted
    - required facts >=90% present in context when needed
    - prompts stay <=4096 normal steps
    - crash/resume matrix passes
    - deterministic tests green

If final evidence is incomplete, report PARTIAL, not PASS.

Do not implement embeddings or page deltas unless the evidence shows actual retrieval misses due to lexical mismatch. PRESENT_NOT_APPLIED does not justify embeddings.
```

