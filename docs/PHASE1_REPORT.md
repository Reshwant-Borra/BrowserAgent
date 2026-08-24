# Phase 1 Report — Basic Local Browser Agent

## Verdict: **PARTIAL**

The deterministic machinery (observation, schema/grammar validation, action execution,
context assembly) is built, tested, and passing. The one thing Phase 1 exists to answer —
*"can Qwen3-8B reliably choose actions from a compact observation?"* — is **not yet
answered**, because no local llama.cpp server was available in this environment to test
against. That is reported honestly below rather than papered over; see "What Is Not Yet
Proven."

## What was built

- `inference/llama_client.py` — httpx client for llama.cpp's native `/completion` endpoint
  (chosen specifically because it accepts a `grammar=` field for GBNF-constrained decoding
  directly, and returns a `timings` block used for instrumentation). Clean diagnostic on
  connection failure (verified — see below), not a raw traceback.
- `inference/grammar/action.gbnf` — constrains the outer JSON shape, the action enum, the
  confidence range, and a length-capped optional `reason` string.
- `agent/schemas.py` + `agent/decision.py` — the full defense-in-depth chain: GBNF → JSON
  parse → Pydantic schema → semantic validation against the *current* observation (stale
  target id, wrong element role for the action, disabled target, invalid URL, invalid select
  option). All layers are unit-tested independently (`tests/unit/test_decision.py`).
- `browser/observer.py` + `browser/page_model.py` + `browser/state_hash.py` — one
  `page.evaluate()` round trip extracts a compact interactive-element list + visible-text
  sample + a normalized state hash. Element identity is a fresh `(css selector, nth-index)`
  pair per observation, never a cached handle.
- `browser/playwright_backend.py` — the nine action verbs (`open_url`, `click`, `type`,
  `select`, `scroll`, `back`, `extract`, `download`, `wait`), using Playwright's native
  actionability checks rather than arbitrary sleeps.
- `agent/context_builder.py` + `inference/prompt.py` — deterministic, byte-identical-given-
  identical-input prompt assembly, ordered static → semi-stable → volatile.
- `agent/loop.py` + `cli/main.py` — the full step loop and `run`/`resume`/`status` CLI.

## Test results

```
tests/unit          52 passed
tests/integration    5 passed  (tests/integration/test_phase1_browser_actions.py specifically)
tests/model          1 skipped (no live llama.cpp server reachable — see below)
```

(Full suite counts, including Phase 2/3 tests that also exercise this same Phase 1 code
path, are in `docs/PHASE3_REPORT.md`'s final tally.)

Phase 1 exit-gate checklist:

1. **Local Qwen endpoint integration works** — the client itself works (verified: it produces
   the exact required diagnostic below when no server is running); **actual integration with
   a running Qwen3-8B server was not exercised** in this environment.
2. **Malformed model output cannot execute** — PASS. `tests/unit/test_decision.py` covers
   malformed JSON, unsupported actions, missing/stale/wrong-typed targets, invalid confidence,
   invalid URLs, and invalid select options — none reach the browser.
3. **Compact page extraction works** — PASS. `tests/unit/test_observation.py` verifies
   button/link/textbox/select/heading extraction, that disabled elements are flagged (not
   excluded), and that hidden elements never appear.
4. **Element IDs map correctly** — PASS. `tests/unit/test_element_mapping.py` verifies a
   target id resolves to the *correct* element among visually-similar siblings.
5. **Browser actions work** — PASS. `tests/integration/test_phase1_browser_actions.py`
   exercises open/click/type/select/download/multi-page-navigation against the real fixture
   site via real Playwright.
6. **Completes several local deterministic Tier-1/2 tasks** — PASS, via scripted decisions
   (see "What scripted integration tests do and don't prove" below).
7. **Automated tests exist** — PASS.
8. **All tests pass** — PASS (68/68 deterministic tests; 1 model test cleanly skipped).

### Manual diagnostic verification

```
$ browser-agent run "Find the assignment"
Local model endpoint unavailable:
http://127.0.0.1:8080

Start llama.cpp server before running the agent.
```

Confirmed by direct invocation (no live server running) — clean message, exit code 1, no
traceback.

## What scripted integration tests do and don't prove

Every integration test in `tests/integration/` uses `ScriptedLlamaClient`
(`tests/integration/fake_llama.py`) instead of a live model — it returns pre-written JSON
decisions in sequence. This proves the deterministic machinery around the model call (
observation extraction, validation, execution, verification, event sourcing, recovery)
works correctly. **It proves nothing about whether Qwen3-8B itself can choose those actions
correctly.** That is a separate, unanswered question — see below.

## What is NOT yet proven (honest gap)

No local llama.cpp server or Qwen3-8B GGUF model was available in this development
environment. Consequently:

- `tests/model/test_live_smoke.py` (the one test that calls a live model) **skipped**, not
  passed — `pytest -m model` output: `1 skipped, 65 deselected`.
- **No measurement exists** of Qwen3-8B's actual success rate at choosing correct actions
  from the compact observation format, its GBNF-constrained output reliability in practice,
  or its real inference latency/token counts.
- `benchmarks/smoke_tasks.yaml`'s five tasks have not been run against a live model.

This is the single most important open item before Phase 1 can be called complete — see
`docs/PHASE1_REPORT.md`'s own verdict (**PARTIAL**) and "Next Recommended Phase" in the
final response.

## Context sizes and latency (measured, from a scripted run — see caveat)

Captured via `tests/_manual_metrics_probe.py` (a one-off script that runs the exact
`test_navigate_click_and_read_price` scenario and prints `metrics.jsonl`):

| step | action | real prompt chars | page char_count | element_count | action latency (ms, real Playwright) |
|---|---|---|---|---|---|
| 1 | open_url | 1685 | 56 (blank start page) | 0 | 68.1 |
| 2 | click | 2135 | 478 | 12 | 42.1 |
| 3 | finish | 1851 | 161 | 1 | — |

**Caveat — read carefully before citing these numbers elsewhere:** the *prompt character
counts* and *action latencies* above are real (the context builder and Playwright backend
are the real production code, unmocked). The `prompt_tokens`/`output_tokens`/`total_latency_ms`
fields in `metrics.jsonl` are **not** real — they come from `ScriptedLlamaClient`, which fakes
a token count as `len(text) // 4` and a fixed 1ms latency, since there is no real tokenizer
or model in this environment. Do not treat those two fields as measured Qwen3 behavior. A
rough chars→token estimate (÷4, standard English approximation, not Qwen's actual tokenizer)
puts these three prompts at roughly 420-535 estimated tokens — comfortably inside
ARCHITECTURE.md's ~3.5-6K normal-mode target, but this is an estimate, not a measurement.

## Observed model failures

None recorded — no live model was run. This section will be populated once
`benchmarks/smoke_tasks.yaml` is run against a real Qwen3-8B server (Phase 4 prerequisite,
not Phase 1-3 scope per the build brief).

## Unresolved issues

- Live-model validation is the main open item (above).
- The GBNF grammar (`inference/grammar/action.gbnf`) constrains the outer JSON shape and
  action enum but not per-action `params`/`expected_result` field shapes (documented, deliberate
  scope choice — Pydantic + semantic validation is the layer that catches those). Whether
  Qwen3-8B stays within the intended per-action param shapes even without grammar-level
  enforcement is untested without a live model.
- `browser/observer.py`'s accessible-name computation is a practical approximation of the
  ARIA accessible-name algorithm (aria-label → aria-labelledby → associated `<label>` →
  placeholder → title → text content), not a full spec implementation — acceptable for v1,
  worth flagging if a target site's naming doesn't match model expectations.
