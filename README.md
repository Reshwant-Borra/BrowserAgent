# browser-agent

A fully local, small-LLM-driven browser automation agent. Phases 1-3 (see
[`ARCHITECTURE.md`](ARCHITECTURE.md) and [`docs/IMPLEMENTATION_PLAN_PHASES_1_3.md`](docs/IMPLEMENTATION_PLAN_PHASES_1_3.md)):
a compact-observation decision loop, deterministic verification/recovery, and event-sourced
crash-safe state — driven by Qwen3-8B running locally via Ollama or llama.cpp, with
Playwright as the browser backend.

## Quick Start

Once installed (see below) and with Ollama running `qwen3:8b`:

```powershell
browser-agent browser start                                                  # once, persistent Chromium
browser-agent ui --browser-mode cdp_attach --cdp-endpoint http://127.0.0.1:9222
```

Then open the printed URL (default `http://127.0.0.1:8765`), type a task in plain English —
e.g. "Check these URLs and tell me which assignments I still have to do: https://..." — and
press **Run**. The persistent-browser Chromium window stays open (and your logins with it)
across BrowserAgent restarts. For quick one-off testing, `browser-agent ui` alone launches
and manages its own throwaway browser instead. See
[`docs/USING_BROWSERAGENT.md`](docs/USING_BROWSERAGENT.md) for the full walkthrough
(approvals, manual login, stop/resume). The CLI commands below remain available for
scripted/power-user workflows.

## Requirements

- Python 3.11+
- [Playwright](https://playwright.dev/python/) (installed as a dependency; browser binaries installed separately, see below)
- [Ollama](https://ollama.com/) or [llama.cpp](https://github.com/ggml-org/llama.cpp)
- Qwen3-8B (`qwen3:8b` in Ollama, or a Q4_K_M/Q5_K_M GGUF for llama.cpp)

## macOS / Apple Silicon

```bash
git clone https://github.com/Reshwant-Borra/BrowserAgent.git
cd BrowserAgent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

brew install ollama
brew services start ollama
ollama pull qwen3:8b
```

Optional llama.cpp/Metal path:

```bash
llama-server -m /path/to/qwen3-8b-Q4_K_M.gguf --host 127.0.0.1 --port 8080
BROWSER_AGENT_MODEL__BACKEND=llama_cpp browser-agent run "Find the assignment"
```

## Windows

```powershell
git clone https://github.com/Reshwant-Borra/BrowserAgent.git
cd BrowserAgent

py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

Install Ollama for Windows, then:

```powershell
ollama pull qwen3:8b
```

Optional llama.cpp/CUDA path:

```bash
llama-server -m /path/to/your/qwen3-8b-Q4_K_M.gguf --host 127.0.0.1 --port 8080
```

No Docker or WSL is required.

## Local Model Backends

`config/default.yaml` defaults to Ollama:

```bash
model:
  backend: "ollama"
  endpoint: ""
  ollama_endpoint: "http://127.0.0.1:11434"
  llamacpp_endpoint: "http://127.0.0.1:8080"
  model_name: "qwen3:8b"
```

Use environment overrides to switch machines or backends without editing source:

```bash
BROWSER_AGENT_MODEL__BACKEND=ollama pytest -m model
BROWSER_AGENT_MODEL__BACKEND=llama_cpp BROWSER_AGENT_MODEL__LLAMACPP_ENDPOINT=http://127.0.0.1:8080 pytest -m model
```

Ollama uses native JSON Schema structured output for action decisions; llama.cpp uses the
repository GBNF grammar. The agent loop talks to a single inference-client abstraction.

## Running the test suite

```bash
pytest -m "not model"     # deterministic unit + integration tests (no live model server needed)
pytest -m model           # live smoke test against the configured local model backend
```

Integration tests spin up Playwright against a local static fixture site
(`tests/fixtures/simple_site/`, served by `tests/conftest.py`'s `fixture_site_url` fixture)
and drive the real agent loop with a *scripted* (non-live) model client
(`tests/integration/fake_llama.py`) — this proves the deterministic machinery (observation,
verification, recovery, event sourcing) works correctly without depending on model quality.
Only `pytest -m model` tests exercise an actual local-model call end-to-end.

## Starting an agent task

```bash
browser-agent run "Find the assignment"
```

Prints the new `task_id` and the final status once the task completes, blocks, or the
step budget (`--max-steps`, default 200) is exhausted.

## Resuming a task

```bash
browser-agent resume <task_id>
```

Reloads persisted state (rebuilding it from the event log if the derived `task_state` row is
missing or stale), reconciles any action that was ambiguously interrupted by a prior crash
(see `agent/loop.py`'s `_reconcile_pending_intent`), reconnects the persistent browser
profile, and continues.

## Inspecting task status

```bash
browser-agent status <task_id>
```

## Configuration

See `config/default.yaml`. Any value can be overridden with an environment variable of the
form `BROWSER_AGENT_<SECTION>__<KEY>` (e.g. `BROWSER_AGENT_BROWSER__HEADLESS=true`).

## Known limitations (intentional — see ARCHITECTURE.md for the phased roadmap)

Phases 1-3 deliberately do **not** include:

- vision / screenshot-based observation (accessibility/interactive-element extraction only)
- embeddings or any vector search
- semantic long-term memory (facts/skills beyond the current task's event log)
- page-delta/diff observations (every step re-extracts a full compact observation; this is
  intentional so token-cost data can be gathered before deciding whether deltas are worth
  the added drift risk — see ARCHITECTURE.md §"Page deltas")
- website-specific skill learning
- a larger secondary "planner" model or planner/executor model-swapping
- fine-tuning of any kind

These are explicitly out of scope for this round and belong to Phase 4+, to be justified by
the metrics this implementation now collects (see `docs/PHASE1_REPORT.md`).

## Known operational constraints

- One task = one browser profile directory (`runtime/tasks/<task_id>/browser_profile`);
  running the same task_id concurrently from two processes will contend for Playwright's
  persistent-context profile lock.
- The CLI's consequential-action approval prompt (`input()`) blocks the event loop while
  waiting for a human response — acceptable since only one task runs per process.
- `runtime/` (browser profiles, task databases, logs, downloads) is gitignored; never commit it.
