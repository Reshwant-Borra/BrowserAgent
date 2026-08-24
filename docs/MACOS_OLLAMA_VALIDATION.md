# macOS Ollama Validation

Date: 2026-08-24

## Machine

- Machine: Apple Silicon Mac, reported chip `Apple M5`
- Architecture: `arm64`
- Unified memory: 24 GB
- macOS: 26.5.1, build 25F80
- Project Python: 3.12.10 in `.venv`
- Homebrew: 6.0.17
- Git: 2.50.1 (Apple Git-155)
- Playwright: 1.62.0, Chromium 151.0.7922.34

## Repository

- Remote: `https://github.com/Reshwant-Borra/BrowserAgent.git`
- Branch before changes: `main`
- Commit before changes: `5d8a37d feat: establish local browser agent phases 1-3`
- Initial deterministic baseline: `68 passed, 1 deselected`

## Setup

Created a fresh macOS virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Installed Ollama with Homebrew and started the local service:

```bash
brew install ollama
brew services start ollama
```

The Ollama API responded locally at `http://127.0.0.1:11434/api/version`.

## Model

- Ollama version: 0.32.15
- Model tag: `qwen3:8b`
- Ollama model ID: `500a1f067a9f`
- Downloaded size: 5.2 GB
- Parameters: 8.2B
- Quantization: Q4_K_M
- Model context length: 40960
- BrowserAgent runtime context configured for Ollama: 8192
- Runner command observed: local llama-server bound to `127.0.0.1`
- Runner RSS during benchmark: about 6.25 GB

Raw inference check succeeded:

- Prompt: `Reply with exactly: ok`
- Response: `ok`
- First-call load duration: 1618 ms
- Prompt tokens: 21
- Output tokens: 2

## Implementation Findings

The existing inference path was hard-wired to `LlamaClient`. The compatibility change adds:

- `model.backend`, defaulting to `llama_cpp`
- `LlamaClient` retained for llama.cpp `/completion` with GBNF
- `OllamaClient` added for Ollama `/api/generate`
- `create_inference_client(config)` used by the agent loop and CLI
- Ollama action decisions use JSON Schema structured output
- Ollama recovery replans use JSON mode, matching the existing llama.cpp path where no action grammar is passed

Architectural difference: Ollama JSON Schema structured output is not identical to llama.cpp GBNF-constrained decoding. It constrains the response shape, but action-specific constraints still depend on JSON parsing, Pydantic, and semantic validation. The executor still only receives decisions after those validation layers.

## Test Results

Deterministic baseline before Ollama changes:

```text
68 passed, 1 deselected in 12.10s
```

After Ollama integration:

```text
pytest -m "not model"
68 passed, 1 deselected in 8.49s
```

Model-backed test:

```text
pytest -m model
1 passed, 68 deselected in 31.15s
```

Full suite with Ollama configured:

```text
pytest
69 passed in 58.69s
```

## Structured Output Probe

Initial Pydantic-derived schema allowed top-level defaults to be omitted. That differed from the existing GBNF outer contract. After requiring the GBNF-equivalent top-level fields, a focused probe produced:

- 15 structured calls
- 12 semantically valid decisions
- 3 semantic validation failures
- Repeated failure: `open_url` included a non-null target
- Average latency: 2881 ms

The stricter schema fixed omitted top-level fields for `type` and `select`, but did not make Qwen reliably choose the correct action.

## Smoke Benchmark

Final run: `runtime/benchmark_runs/qwen3_8b_final_3x`

Configuration:

- Backend: `ollama`
- Model: `qwen3:8b`
- Temperature: 0.1
- Context window: 8192
- Headless browser: true
- Human intervention: none

| Task | Trials | Passes | Avg Actions | Avg Model Calls | Recoveries | Main Failure |
| ---- | -----: | -----: | ----------: | --------------: | ---------: | ------------ |
| tier1_products_price | 3 | 0 | 9 | 9 | 18 | MODEL |
| tier2_search_and_open_result | 3 | 0 | 8 | 8 | 27 | VERIFICATION |
| tier2_settings_dropdown | 3 | 0 | 1 | 10 | 3 | MODEL |
| tier2_download_file | 3 | 0 | 1 | 10 | 3 | MODEL |
| tier3_wizard_no_overstep | 3 | 0 | 3 | 5 | 9 | MODEL |

Aggregate final benchmark metrics:

- Task trials: 15
- Passed: 0
- Statuses: 12 `running`, 3 `blocked`
- Model calls: 126
- Browser actions: 66
- Decision validation errors: 57 `missing_target`
- Average prompt tokens: 509.90
- Average output tokens: 44.49
- Average prompt evaluation duration: 426.48 ms
- Average generation duration: 3248.71 ms
- Average model latency: 3702.33 ms
- Average prompt processing speed: 1195.60 tokens/s
- Average generation speed: 13.69 tokens/s
- Average browser action latency: 18.61 ms
- Average observation characters: 141.98
- Average interactive elements: 1.33
- Average task duration: 34.19 s
- Total benchmark duration: 512.85 s

## Failure Analysis

OBSERVATION:

- Not the primary failure. The compact observations contained the relevant controls and text for the fixture tasks. Average observations were small.

MODEL:

- Primary failure category.
- Qwen repeatedly understood the intended control semantically but did not place its numeric ID in the top-level `target`.
- Settings and download each produced 9 `missing_target` errors per trial after the initial `open_url`.
- Products reached the correct page and extracted the right text repeatedly, but did not choose `finish`.
- Wizard reached the confirmation page, then tried or reasoned toward a final submit-style action and blocked instead of finishing.

PROMPT:

- Likely contributing. The prompt states the target contract, but Qwen still often used natural names or params instead of top-level numeric IDs.

SCHEMA:

- The initial Ollama schema was too permissive relative to GBNF. Requiring top-level fields fixed one integration mismatch.
- JSON Schema alone still does not express action-dependent target requirements.

EXECUTION:

- Browser execution worked. Navigations, clicks, and page observations occurred with low browser latency.

VERIFICATION:

- Secondary issue in search. Some actions executed but expected-result assertions were brittle or wrong, causing recovery despite progress.

RECOVERY:

- Recovery did not rescue repeated model contract failures.
- Replan needed an Ollama-specific fix so it did not use the action schema.

COMPLETION:

- Products and wizard show completion recognition failures. The model reached or observed enough evidence but did not reliably emit `finish`.

## Phase 1 Verdict

FAIL

Qwen3-8B on this M5/24 GB Mac can run locally, BrowserAgent can call it through Ollama, and the deterministic browser-control foundation remains intact. However, the local 8B model did not reliably complete any existing smoke task across 15 final trials using compact observations and the existing constrained action API. The foundation works mechanically, but the current model/action-contract/prompt combination is not a reliable browser agent baseline.

## Phase 4 Direction

Do not start with memory, embeddings, screenshots, or multi-agent routing. The measured blocker is basic action-contract compliance and completion recognition.

Recommended next work:

1. Tighten action-specific structured schemas or split action selection from action-argument filling.
2. Add targeted prompt experiments around numeric element IDs and `finish` conditions.
3. Improve verifier expectations so correct progress is not converted into recovery loops.
4. Re-run the same smoke suite before considering larger architectural additions.
