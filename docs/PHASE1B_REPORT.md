# Phase 1B Report: Model/Tool Contract Reliability

## Baseline

- Baseline commit: `2e7ade79a9a4137793891402d93cb94867acfce3`
- Baseline pushed: `origin/main` at `2e7ade79a9a4137793891402d93cb94867acfce3`
- Baseline benchmark directory: `runtime/benchmark_runs/qwen3_8b_final_3x/`
- Baseline result: `0/15`
- Baseline verdict: `FAIL`

The baseline failures were dominated by the model/action contract and completion behavior,
not Playwright, persistence, event sourcing, Ollama installation, or macOS.

## Experiments

| ID | Directory | Main Change | Result |
| --- | --- | --- | --- |
| A | `qwen3_8b_final_3x` | Existing universal schema | `0/15` |
| B | `phase1b_b_action_specific` | Action-specific schema, target rules, examples, deterministic verifier defaults | `12/15`; download reached environment success but did not finish |
| C | `phase1b_c_download_semantic_repair` | Generic download-action semantic rule | `12/15`; search regressed via repeated same-value typing |
| D | `phase1b_d_value_noop_repair` | Render input/select values; reject repeated same-value typing | `15/15` |
| E | `phase1b_e_completion_history` | Add sanitized result details to recent action history | `15/15`; final reported run |

Two-stage action/argument generation was not run because the one-call discriminated schema
had no meaningful syntax/schema/semantic failures in the live structured-output probe.

## Structured Output Probe

Directory: `runtime/benchmark_runs/phase1b/probe_action_specific_value_noop/`

| Calls | Syntax Valid | Schema Valid | Semantic Valid | Task-Correct |
| ---: | ---: | ---: | ---: | ---: |
| 30 | 100% | 100% | 100% | 100% |

The probe covered all action classes: `open_url`, `click`, `type`, `select`, `scroll`,
`back`, `extract`, `download`, `wait`, and `finish`.

## Results

Primary final directory: `runtime/benchmark_runs/phase1b/phase1b_e_completion_history/`

| Task | Passes | Environment Success | Model Finish | Main Remaining Failure |
| --- | ---: | ---: | ---: | --- |
| `tier1_products_price` | 3/3 | 3/3 | 3/3 | none |
| `tier2_search_and_open_result` | 3/3 | 3/3 | 3/3 | none |
| `tier2_settings_dropdown` | 3/3 | 3/3 | 3/3 | none |
| `tier2_download_file` | 3/3 | 3/3 | 3/3 | inefficient repeated downloads before finish |
| `tier3_wizard_no_overstep` | 3/3 | 3/3 | 3/3 | none |

Final smoke result: `15/15`.

## Holdout

Holdout tasks were added after finalizing the Phase 1B design.

Directory: `runtime/benchmark_runs/phase1b/phase1b_holdout_final_strict/`

| Task | Result |
| --- | ---: |
| `holdout_docs_from_home` | 1/1 |
| `holdout_products_pro_price` | 1/1 |
| `holdout_search_beta` | 1/1 |
| `holdout_preferences_compact` | 1/1 |
| `holdout_download_report` | 1/1 |

Holdout result: `5/5`.

## Performance

| Metric | Baseline | Phase 1B Final |
| --- | ---: | ---: |
| Full task passes | 0/15 | 15/15 |
| Environment success | 0/15 | 15/15 |
| Model finish | 0/15 | 15/15 |
| Avg model calls/task | 8.40 | 4.47 |
| Avg actions/task | 4.40 | 3.47 |
| Avg prompt tokens/call | 509.9 | 716.5 |
| Avg output tokens/call | 44.5 | 15.7 |
| Avg model latency/call | 3702 ms | 929 ms |
| Avg task duration | 34.2 s | 4.9 s |
| Recoveries | 60 | 12 |

Prompt tokens increased because the contract is more explicit and includes examples. Output
tokens and latency fell because the model emits small action-specific objects and takes
fewer calls per task.

## Failure Taxonomy

Resolved or materially reduced:

- `MODEL_TARGET_BINDING_ERROR`: explicit target rules and action-specific schemas.
- `MODEL_PARAMETER_ERROR`: action-specific fields and option validation.
- `SCHEMA_SEMANTIC_ERROR`: target existence/type checks, same-value type no-op rejection,
  and download-action semantic validation.
- `MODEL_COMPLETION_ERROR`: finish prompt rule plus environment/model-finish reporting.
- `VERIFICATION`: deterministic action-level verifier defaults for `open_url`, `type`,
  `select`, `click`, `download`, and `back`.

Remaining:

- Download tasks still repeat the verified download several times before `finish`, causing
  12 recovery transitions across 3 trials. This is a completion inefficiency, not a task
  failure or browser execution failure.

## Verifier Findings

Objectively wrong or brittle verifier behavior:

- `type` should verify the target value, not a model-generated page assertion.
- `select` should verify the selected label/value, not an invented semantic assertion.
- `download` should verify the Playwright download event/file.
- Environment success and model finish must be reported separately.

Kept strict:

- The smoke task list was not changed.
- Success criteria were not weakened.
- No deterministic target auto-fill was added.

## Final Verdict

`PASS`

Phase 1B shows Qwen3-8B can be a reliable browser executor for this suite when the
model/tool interface is engineered as an action-specific contract with explicit target
binding, semantic validation, deterministic verifier defaults, and completion accounting.
