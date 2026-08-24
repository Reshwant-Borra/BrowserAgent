# Local Browser-Agent Architecture: Research Report & Design

**Target hardware:** single consumer GPU, 8–12GB VRAM.
**Objective:** a fully local, small-model (7–14B) browser-automation agent capable of long-horizon tasks (hundreds of actions) without unbounded context growth destroying latency, VRAM, or reliability.

This report was produced by treating the author's own starting hypothesis (clear-context-and-reload-from-`memory.md`) as a claim to falsify, not a conclusion to justify. Two of six core hypotheses were overturned by evidence gathered from primary sources (papers, official repos/docs) during this pass. Section 2 details exactly what changed and why.

---

## 1. Executive Verdict

**Recommended architecture: a hybrid deterministic/LLM system with event-sourced state, tiered (not disposable) context, hybrid BM25+embedding retrieval, hierarchical planning, and mandatory action verification.**

The single biggest correction from the original hypothesis: **do not fully discard LLM context and rebuild it from scratch every call.** Every real system examined that solves this problem at scale (MemGPT/Letta, Claude Code, OpenHands, SWE-agent) keeps a **continuous, prefix-stable context that is selectively compacted**, not periodically nuked and rebuilt. This isn't just an empirical preference — it's also a hard efficiency constraint on local inference: `llama.cpp`'s prompt/KV-cache reuse only works when consecutive calls share a stable prefix. A "disposable" architecture that reassembles a differently-shaped context every call defeats prefix caching and pays full prompt-prefill cost on every single action — a direct VRAM/latency cost, not just a reliability one.

```
User goal
   ↓
Task Manager (event-sourced, deterministic)
   ↓
Memory Router (BM25 + small local embedding, hybrid, deterministic)
   ↓
Context Builder (stable prefix + condensed summary + recent-turn window + retrieved memory + page state)
   ↓
Local LLM (Qwen3-8B via llama.cpp, GBNF-constrained JSON output)
   ↓
Action Executor (idempotency-checked, Playwright-backed)
   ↓
Browser
   ↓
State Extractor (AX-tree/interactive-element list; screenshot only on escalation)
   ↓
Verifier (expected vs. actual, deterministic)
   ↺ (append event, update task_state, decide: continue / replan / escalate / ask user)
```

This differs from the user's original proposal mainly in **what persists between calls** (event log + prefix-stable running context, not a periodically-cleared chat plus a markdown file) and **what retrieval uses** (hybrid lexical+embedding, not markdown re-read in full). It keeps the parts of the original idea that evidence actually supports: structured, externalized memory; aggressive compression of browser observations; and treating the LLM as the expensive, scarce resource that deterministic code should shield.

---

## 2. Why the Markdown-Memory Idea Is/Isn't Optimal

**What was right:**
- **Externalizing state outside the model's context is correct.** Every serious system (MemGPT, OpenHands, browser-use) externalizes memory rather than trusting an ever-growing prompt.
- **The instinct that context must not grow unboundedly is correct** — ReAct's undocumented failure mode (context saturates, no compaction) is exactly the problem every downstream system (OpenHands's condenser, Claude Code's compaction, SWE-agent's history processor) was built to fix.
- **Treating "what should the model see right now" as a deliberately engineered, minimal question is correct** — this is the core idea behind AgentOccam's biggest performance win (arXiv:2410.13825) and BrowserGym's structured observation design.

**What was wrong / falsified:**
- **"Fully clear the context and reload from memory.md" is not what any examined production or research system does.** MemGPT explicitly pages memory in/out rather than discarding (arXiv:2310.08560); Letta's archival memory is embedding-indexed, not a flat file re-read wholesale; Claude Code, OpenHands, and SWE-agent all keep a live, continuous history and apply *selective* compaction (summarize/truncate/elide) rather than full resets. No system examined treats "wipe everything, reload a document" as sound practice for a task still in progress.
- **A single flat `memory.md` re-read in full each cycle doesn't scale token cost with relevance** — it grows linearly with task length exactly like the chat history it's meant to replace, just moved to a different file. It also has no retrieval discrimination: the whole file goes back in regardless of what's relevant to the current subgoal.
- **Markdown prose is a bad fit for anything that needs to be queried, diffed, or verified by code** (loop detection, "have I already tried this," idempotency checks, provenance tracking). Structured formats (JSON state + SQLite events) support this; prose does not, and re-parsing markdown with the LLM itself to extract facts wastes the exact resource (LLM calls) the architecture is trying to conserve.
- **Full reset also throws away KV-cache reuse** on local inference engines — a concrete VRAM/latency cost specific to the local-model context that wouldn't show up in a cloud-API-only discussion of the idea.

**What to keep from the original idea, revised:**
| Original | Revised |
|---|---|
| `memory.md`, fully reloaded | `task_state.json` (derived view) + append-only `events` table (source of truth) |
| Clear context every N actions | Never fully clear; append + periodically compact the *middle* of the context, keep the stable prefix and a recent-turn window intact |
| One flat memory file | Tiered: working (task_state) / episodic (event log) / semantic (facts, hybrid-retrieved) / procedural (skills) |
| Re-read everything on reload | Router retrieves only what's relevant to the current subgoal (BM25 + embedding, deterministic) |

---

## 3. Approaches Investigated

- **A — Continuous conversation:** simplest, but reproduces ReAct's documented failure mode (unbounded growth, eventual context-window overflow) with no compaction.
- **B — Markdown memory (user's original hypothesis):** simple and debuggable, but falsified as the primary mechanism per Section 2 — retained as a *human-readable export/debug view* of the real state, not as the system of record.
- **C — Structured state only (task_state.json + last-N actions + current page):** better than B for machine use, but evidence (Section 4) shows pure structured-state without any retained recent raw context loses signal small models rely on implicitly.
- **D — Retrieval memory (structured state + SQLite + retrieved memories):** correct direction; evidence shows retrieval should be hybrid (BM25 + embeddings), not BM25-only.
- **E — Hierarchical agent (planner/subgoal-manager/executor/verifier):** strongly supported — AgentOccam and BrowserGym both show observation/action-space and task decomposition design dominate raw model scale for this problem class.
- **F — Hybrid deterministic/LLM system:** the winning direction, refined below to add event sourcing, a tiered (not disposable) context, hybrid retrieval, confidence-driven observation escalation, and idempotent actions.
- **G — MemGPT/Letta-style OS-paging memory:** the closest existing reference architecture to what the evidence actually supports; adopted as the model for the memory subsystem specifically (Section 8), while diverging from it on browser-specific concerns (observation compression, action verification) that MemGPT doesn't address.

---

## 4. Evidence From Existing Systems and Research

### Observation: AX-tree/text vs. vision
- **SeeAct** (arXiv:2401.01614) — the paper most associated with vision-first web agents — found **set-of-mark (SoM) visual grounding underperforms HTML/DOM-informed textual grounding** by a large margin; its best-performing grounding strategy is textual, not pixel-based.
- **AgentOccam** (arXiv:2410.13825, ICLR 2025) — targeting small/plain LLMs specifically — got its largest gains (+9.8 pts / 29.4% relative on WebArena over prior SOTA) purely from refining the **text-based** observation/action space, no vision involved.
- **WebVoyager** (arXiv:2401.13919) and the **Windows Agent Arena / Mind2Web** results (arXiv:2409.08264) show vision-augmented GPT-4-class agents beating pure-text baselines in aggregate — but these are large-model results, admit weakness on text-heavy pages, and no controlled small-model (7–14B) study was found either way.
- **BrowserGym** (arXiv:2412.05467) defaults to a hybrid DOM+AXTree structured representation (via CDP), with vision as optional, not primary.
- **Verdict: AX-tree/text-as-default survives.** Vision is a targeted escalation path (canvas apps, CAPTCHAs, icon-only UI), not a general replacement — consistent with the confidence-driven escalation design in Section 9, not with routine vision-model use.

### Browser backend: Playwright vs. alternatives
- **browser-use**, a major agent framework built on Playwright, published an explicit account of **migrating away from Playwright to raw CDP** ("Closer to the Metal," browser-use.com/posts/playwright-to-cdp) — citing an extra network hop (Node.js relay), state drift across three runtimes, large-screenshot crashes, and gaps in dialog/cross-origin-iframe handling once call volume got high.
- **Playwright MCP** (Microsoft, github.com/microsoft/playwright-mcp) defaults to accessibility-tree snapshots specifically to avoid vision-model cost, and provides Playwright's native **actionability checks** (attached/visible/stable/enabled/receives-events with auto-retry) — real, documented flakiness reduction that raw CDP does not give you for free.
- **BrowserOS** is an AGPL Chromium-based product bundling agent workflows and local-model support in the browser chrome itself, not a documented, minimal backend abstraction for a custom agent — thin third-party integration docs relative to Playwright/CDP.
- **No framework (Playwright, CDP, Puppeteer) provides native semantic idempotency** (i.e., "did this click actually submit the form, is retrying safe") — this must be hand-built regardless of backend, via before/after AX-tree diffing.
- **Verdict: Playwright weakened, not falsified.** Recommend Playwright (ideally via Playwright MCP) as the v1 backend for actionability guarantees and development velocity, with a documented, evidence-backed escape hatch to raw CDP if/when per-step latency at high call volumes becomes the bottleneck — exactly the trigger that made browser-use switch.

### Context: disposable/stateless vs. continuous+compacted
- **MemGPT** (arXiv:2310.08560) explicitly rejects full discard: it pages data between "main context" and "external context" like OS virtual memory, because the system cannot know in advance what becomes relevant again. **Letta**'s production implementation confirms archival memory is embedding-indexed and paged, never simply dropped.
- **Reflexion** (arXiv:2303.11366) keeps an episodic reflection buffer across attempts; full Reflexion (reflection + episodic memory) beat trajectory-memory-only by 8 points absolute — continuity beat discard-and-reload.
- **ReAct** (arXiv:2210.03629) has no compaction at all — unbounded accumulation until context overflow is its documented failure mode, i.e., the problem, not a design others copied.
- **Claude Code** uses three-tier compaction (microcompact for stale tool results, LLM-generated summary near the context ceiling, manual `/compact`) — selective, not full-reset.
- **OpenHands**'s `LLMSummarizingCondenser` truncates and replaces dropped events with a goal/progress/critical-files summary, keeping recent turns intact.
- **SWE-agent**'s `last_n_observations` history processor elides all but the last N observations (blanking stdout, keeping actions/thoughts) — partial retention.
- **Verdict: falsified.** No examined system treats "discard everything, rebuild from structured state alone" as a winning pattern. The consistent pattern across all of them is: **keep a stable/append-only backbone, compact the middle selectively, keep the recent tail raw.**

### Retrieval: BM25/FTS5 vs. embeddings
- BM25's classic failure mode is synonymy/paraphrase (e.g., "automobile" vs. "car" doesn't match lexically).
- **MemGPT/Letta — the field's reference agent-memory implementation — uses embeddings (pgvector/HNSW, `bge-small-en-v1.5`), not BM25**, for archival memory.
- Hybrid BM25+embedding (reciprocal rank fusion) is the broadly recommended production pattern; a large-scale RAG study found lexical/agentic search only wins over dense retrieval past ~10M corpus tokens — well beyond the hundreds-to-low-thousands of entries a single browser agent accumulates.
- **Verdict: falsified as sufficient alone.** A small local embedding model (e.g., `bge-small-en-v1.5`, ~130MB, CPU-cheap) plus SQLite FTS5, fused, is the evidence-supported design — not pure BM25, and not a heavyweight vector database either (see Section 22).

### Event sourcing & crash recovery
- **OpenHands** implements genuine event sourcing: an append-only `EventLog` is the source of truth; agent/task state is a derived, replayable view; resuming means loading a base state and replaying events, with automatic detection of incomplete conversations (docs.openhands.dev/sdk/arch/events).
- **LangGraph** checkpoints derived state snapshots per step (not an append-only action log) — a documented limitation is that resuming re-executes the interrupted node, re-triggering calls rather than continuing mid-node.
- **SWE-agent** uses trajectory logs, not an explicit event-sourced replay system.
- **Verdict: supports event sourcing as the crash-recovery backbone** (Section 12), following OpenHands's pattern rather than LangGraph's snapshot-only pattern.

### Models & runtime
- **No independent benchmark found that dethrones Qwen3 in the 7–14B agentic/tool-use class** — Qwen3-8B/14B appear repeatedly as the substrate for 2026 agentic-tuning papers (arXiv:2601.15625, arXiv:2511.15718), and no competing model (Llama 3.x small, Ministral, Gemma 3, GLM-4-9B) has a verified independent head-to-head win at matched size. This is a **gap in available evidence, not a confirmed win** — the live BFCL leaderboard table itself could not be fetched/verified directly.
- **DeepSeek distilled 7B/8B/14B variants are a poor fit** — not tool-use oriented, and DeepSeek's own docs note a known bug where `tool_calls` arrays return empty on distilled variants.
- **llama.cpp has mature native grammar-constrained (GBNF) JSON output** and a working per-slot prompt/KV-cache reuse mechanism (`cache_prompt`, `n_cache_reuse`) purpose-built for repeated shared-prefix calls — directly enabling the prefix-stable context design in Section 9.
- **Ollama** (llama.cpp-based) has a documented bug where KV-cache reuse doesn't function on the CPU backend — GPU-only deployment is required for the caching benefit to materialize.
- **vLLM** has strong structured-decoding (xgrammar) and automatic prefix caching, but is designed for multi-GPU throughput serving; its footprint and GGUF-quantization support are not confirmed as favorable for a single 8–12GB GPU.

---

## 5. Architecture Comparison Matrix

Weights reflect this project's priorities: reliability and small-model-friendliness matter most (a fast system that fails tasks is useless); token/VRAM efficiency matter a lot given the 8–12GB constraint; ease-of-implementation matters because this is a single-engineer build.

| Criterion (weight) | A: Continuous | B: Markdown memory | C: Structured state only | D: Retrieval memory | E: Hierarchical | **F: Hybrid (recommended)** |
|---|---|---|---|---|---|---|
| Token efficiency (10) | 2 | 5 | 7 | 8 | 7 | 9 |
| VRAM efficiency (10) | 2 | 6 | 7 | 8 | 7 | 9 |
| Inference speed (8) | 3 | 6 | 7 | 7 | 7 | 9 |
| Reliability (10) | 3 | 5 | 6 | 7 | 8 | 9 |
| Long-task stability (10) | 2 | 5 | 6 | 7 | 8 | 9 |
| Recovery (9) | 2 | 4 | 5 | 6 | 7 | 9 |
| Small-model friendliness (10) | 2 | 5 | 6 | 7 | 9 | 9 |
| Ease of implementation (6) | 9 | 8 | 7 | 6 | 5 | 5 |
| Extensibility (6) | 3 | 5 | 6 | 7 | 8 | 9 |
| Debuggability (7) | 5 | 8 | 8 | 7 | 7 | 8 |
| Memory accuracy (9) | 4 | 5 | 6 | 7 | 7 | 9 |
| Browser compatibility (6) | 6 | 6 | 6 | 6 | 6 | 7 |
| **Weighted total /1000** | **~272** | **~453** | **~538** | **~618** | **~683** | **~778** |

F wins primarily on reliability, long-task stability, and small-model friendliness — the three criteria most directly evidenced by Section 4 — at a real, acknowledged cost in implementation effort (Section 21 sequences this cost across phases so it isn't paid all at once).

---

## 6. Recommended Architecture

**Hybrid deterministic/LLM system**, five cooperating subsystems:

1. **Task Manager** — owns the event log (source of truth) and the derived `task_state` (materialized view), hierarchical plan/subgoal tracking, retry/backoff counters, checkpointing.
2. **Memory Router** — deterministic hybrid retrieval (SQLite FTS5 BM25 + small local embedding cosine similarity, fused) over episodic/semantic/procedural memory, scoped to the current subgoal + domain.
3. **Context Builder** — assembles a **prefix-stable** context: static system/task/plan block first (maximizes KV-cache reuse), then a periodically-compacted running summary, then a raw recent-turn window, then retrieved memory, then current page state, in that fixed order.
4. **Local LLM** — Qwen3-8B via llama.cpp, GBNF-grammar-constrained to a JSON action schema; escalates to a deeper-reasoning mode or larger context only on low confidence/verification failure.
5. **Action Executor + Verifier** — Playwright-backed, idempotency-checked (pre-action state hash, post-action diff against `expected_result`), with a recovery ladder (retry → refresh state → deep reasoning → replan → ask user).

---

## 7. Information Flow

```
Browser changes (or first observation)
   ↓
State Extractor: compact interactive-element/AX list (+ page hash)
   ↓
Delta engine: same page + hash match on prior baseline → emit diff; else full refresh
   ↓
Memory Router: BM25+embedding retrieve top-k facts/skills for (domain, current_subgoal)
   ↓
Context Builder: [stable prefix][running summary][recent-turn window][retrieved memory][page state][tool schema]
   ↓
Qwen3-8B (llama.cpp, GBNF JSON): { reason, action, target, expected_result, confidence }
   ↓
Executor: idempotency check → execute action (Playwright)
   ↓
State Extractor: re-observe
   ↓
Verifier: expected_result vs. actual (URL/DOM assertions) → PASS / FAIL
   ↓
Task Manager: append event (action, result, verification) → update task_state → checkpoint (fsync)
   ↓
Compactor: if running-summary staleness threshold hit → summarize dropped middle turns (cheap/deterministic-first, LLM fallback)
   ↓
Loop/no-op/navigation-loop detector: check event tail for repetition
   ↓
if FAIL/low-confidence → escalate (fuller AX tree → screenshot+SoM → deeper reasoning → replan → ask user); else continue
   ↺
```

---

## 8. Memory Architecture

**Storage:** SQLite (single file per task/session) — event sourcing as the backbone, following OpenHands's pattern.

**Schema (see Section 17 for full DDL-equivalent):**
- `events` (append-only, source of truth): step, timestamp, type, payload, verification_result.
- `task_state` (derived/materialized, rebuildable by replaying `events`): goal, plan, current_subgoal, completed_subgoals, retry_count, blocked_reason.
- `memories` (semantic facts + episodic summaries), each with `confidence`, `source_event_id`, `created`, `last_verified` — provenance is worth implementing; it's cheap (a few extra columns) and directly prevents silent memory corruption from an LLM-authored fact that turns out wrong.
- `memories_fts` (FTS5 virtual table over `memories.content`) + `memories.embedding` (BLOB, computed with a small local model, e.g. `bge-small-en-v1.5` via `sqlite-vec` or brute-force cosine over a few thousand rows — no external vector DB needed at this scale, see Section 22).
- `skills/<domain>.md` (procedural memory) — the one place Markdown is still the right format: human-authored or auto-compressed reusable workflows per site (Canvas, Gmail, GitHub), read in full only when the router matches the current domain (small, bounded size, unlike the original flat `memory.md`).

**Lifecycle:** working memory (`task_state`) is always in-context; episodic memory (recent events) lives in the recent-turn window until compacted into the running summary; semantic/procedural memory is retrieved on demand, never loaded wholesale. Deletion: episodic raw events can be pruned after summarization once a task completes and its summary is durable; semantic facts are revisited/reconfirmed (`last_verified`) rather than deleted, to avoid re-learning the same thing repeatedly.

---

## 9. Browser Representation

Default: compact interactive-element extraction (browser-use/AgentOccam-style), not raw DOM, not routine screenshots.

```
PAGE: Canvas — Course: CS 101 (https://canvas.instructure.com/courses/4821)
[12] link "Modules"
[13] link "Assignments"
[14] link "Grades"
[41] heading "Week 3: Recursion"
[42] link "Assignment: Recursive Sort (due Sun 11:59pm)"
[43] button "Download materials.zip"
```

**Escalation ladder (confidence-driven, Section 12):**
1. Compact interactive-element list (default, above).
2. Full accessibility tree (if the compact list's confidence is low or an expected element is missing).
3. Screenshot + Set-of-Mark labels (only for canvas/icon-only/CAPTCHA-shaped pages, or after two consecutive verification failures at level 2).

**Delta representation** (same page, minor change — e.g., a dropdown opened):
```
PAGE CHANGES (same page, hash abc123→def456)
+ [83] option "8 GB"
+ [84] option "16 GB"
```
Rules: same page + small change → delta; navigation (URL change) → full refresh; every 20 actions → forced full refresh regardless (drift guard); unexpected/failed verification → full refresh, never trust the delta after a surprise.

---

## 10. LLM Prompt Structure & Token Budget

Ordered for maximum KV-cache prefix reuse (static-to-volatile):

| Block | Normal budget | Notes |
|---|---|---|
| System + role + tool schema | 500 | Static across the whole task — full cache reuse |
| Task + success criteria + plan | 300 | Changes only on replan |
| Running summary (compacted middle) | 400 | Updated only when compaction triggers |
| Recent-turn window (last 3–5 raw actions+results) | 700 | Append-only until compaction |
| Retrieved memory (hybrid top-k) | 400 | Recomputed each call, scoped to subgoal |
| Current page state (compact list or delta) | 1200 | Largest variable cost; capped, escalates on low confidence |
| **Total (normal mode)** | **~3500** | |
| **Max normal** | **~6000** | e.g., unusually large page |
| **Recovery mode** (full AX tree or screenshot+SoM, deeper reasoning) | **10000–16000** | Rare, triggered by verification failure/loop detection, not routine |

Compaction triggers when the running-summary + recent-window combination would exceed ~2500 tokens; retrieval runs every call but is capped and deterministic, not an LLM call.

---

## 11. Browser Tool API

Small, constrained action space — a deterministic router translates these into Playwright calls, not raw CDP exposed to the model:

```
open_url(url)
click(target_id)
type(target_id, text)
select(target_id, value)
scroll(direction)
back()
extract(target_id | "page")
download(target_id)
wait(condition)
finish(result)
```

Every action call is wrapped: `{ "reason": str, "action": str, "target": int, "params": {...}, "expected_result": {...}, "confidence": float }`. GBNF-grammar-constrained via llama.cpp so the shape is guaranteed, not just requested.

---

## 12. Verification and Recovery System

Every action carries `expected_result` (e.g., `{"url_contains": "/assignment/", "page_contains": "Submit Assignment"}`); the Verifier checks it deterministically post-action — never assume execution == success.

**Idempotency:** before executing, hash the pre-action page state; if a retry is triggered and the pre-action hash is unchanged from a previous failed attempt at the same step, treat the action as not-yet-applied (safe to retry); if the hash changed but verification still failed, treat as applied-but-wrong (do not blindly retry a submit/purchase/delete — escalate instead).

**Recovery ladder:**
```
NORMAL
  ↓ (verification fails once)
RETRY (same action, fresh observation)
  ↓ (fails again)
REFRESH STATE (force full page re-extraction, drop delta assumption)
  ↓ (still failing)
DEEP REASONING (escalate context: full AX tree / screenshot+SoM, longer reasoning budget)
  ↓ (still failing)
REPLAN (planner call: new subgoal decomposition)
  ↓ (replan exhausted, or consequential action pending — Section 26)
ASK USER
```

**Failure detectors (deterministic, not LLM-judged):** repeated identical action on identical page-hash (loop); URL cycling A→B→A→B (navigation loop); action executed but page-hash unchanged (no-op); `expected_result` mismatch (unexpected state); stale target id (element not found — force refresh); modal/popup present but not addressed by the last action (dismiss-or-handle before continuing).

---

## 13. Model Recommendation

| Tier | Pick | Why | Caveat |
|---|---|---|---|
| **Primary (8–12GB target)** | **Qwen3-8B**, Q4_K_M/Q5_K_M GGUF | Repeated substrate of choice in 2026 agentic-tuning papers; fits comfortably with headroom for KV cache + a small embedding model at 8–12GB | No independent BFCL/τ-bench win over every competitor was verifiable — this is the best-supported choice given available evidence, not a proven #1 |
| **Upgrade path (16GB+)** | Qwen3-14B, Q4_K_M (~8–8.5GB weights) | Stronger reasoning/planning for the low-frequency planner role | Leaves only ~1–4GB for KV cache at 12GB — workable but tight; better at 16GB+ |
| **Worth trying, unverified vs. Qwen3** | Ministral-8B, GLM-4-9B | Vendor-claimed strong native function-calling | No independent head-to-head found — treat as experiment candidates in the benchmark plan (Section 19), not defaults |
| **Avoid** | DeepSeek distilled 7B/8B/14B | Not tool-use oriented; documented empty-`tool_calls` bug on distilled variants | |
| **Avoid for VRAM budget** | Qwen3-30B-A3B (MoE) | ~18.6GB at Q4_K_M despite low active-param compute — VRAM is dominated by stored weights, not active compute; doesn't fit 8–12GB | |

**Asymmetric planner/executor (optional, Phase 6+):** since planning happens far less often than execution, a larger model (e.g., Qwen3-14B) can be swapped into VRAM only for planner calls, then unloaded in favor of Qwen3-8B for the execution loop — feasible because model-swap latency is amortized over many executor steps per planner call. Not required for v1.

---

## 14. Inference Runtime Recommendation

**llama.cpp** (directly, or via **LM Studio** for a GUI), for two concrete, evidence-backed reasons: mature native GBNF grammar-constrained JSON decoding, and a working per-slot prompt/KV-cache reuse mechanism (`cache_prompt`, `n_cache_reuse`) that directly rewards the prefix-stable context design in Section 10. **Avoid Ollama's default config** unless confirmed GPU-only — its KV-cache reuse is documented as non-functional on the CPU backend, silently eliminating the exact benefit this architecture depends on. vLLM's structured-decoding and auto prefix-caching are real strengths but its footprint/GGUF support are unconfirmed for single 8–12GB-GPU deployment; not recommended for v1.

---

## 15. Browser Backend Recommendation

**Playwright**, ideally via **Playwright MCP** (accessibility-tree-first by default, actionability checks built in) for v1. This is a deliberate, evidence-weighed choice, not a default-by-name-recognition one: browser-use's own migration to raw CDP is real counter-evidence that Playwright's relay layer costs latency/reliability at high call volume — but this project starts at a much lower call rate than a hosted multi-tenant agent service, and Playwright's actionability checks measurably reduce flaky-click failures that would otherwise be misattributed to model error during early development. **Documented escape hatch:** if per-step latency becomes the dominant cost once the agent is running hundreds-of-actions tasks routinely, migrate the executor to raw CDP — following browser-use's own precedent — while keeping the same accessibility-tree-based observation format. BrowserOS is not recommended: it's a browser product with thin third-party agent-integration documentation, not a minimal, well-documented backend abstraction.

---

## 16. Folder / Repository Architecture

```
local-browser-agent/
├── agent/
│   ├── planner.py           # low-frequency subgoal decomposition/replanning
│   ├── executor.py          # per-step action selection (LLM call)
│   ├── verifier.py          # deterministic expected-vs-actual checks
│   ├── context_builder.py   # ordered, prefix-stable context assembly
│   └── recovery.py          # NORMAL→RETRY→REFRESH→DEEP→REPLAN→ASK ladder
├── browser/
│   ├── playwright_backend.py
│   ├── state_extractor.py   # AX/interactive-element extraction, hashing
│   └── delta_engine.py      # page diffing, drift guard
├── memory/
│   ├── event_store.py       # append-only event log, replay
│   ├── task_state.py        # derived/materialized view
│   ├── router.py            # BM25 + embedding hybrid retrieval
│   ├── compactor.py         # running-summary maintenance
│   └── schema.sql
├── skills/
│   ├── canvas.md
│   ├── gmail.md
│   └── github.md
├── models/                  # GGUF weights, not committed
├── schemas/                 # JSON schemas / GBNF grammars for action output
├── tests/
├── benchmarks/              # Section 19 harness
└── logs/                    # per-task event-log SQLite files
```

---

## 17. Data Schemas

```sql
-- events: append-only source of truth
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL,
  step INTEGER NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,           -- 'action' | 'observation' | 'verification' | 'replan' | 'checkpoint'
  payload TEXT NOT NULL,        -- JSON
  verification_result TEXT      -- 'pass' | 'fail' | NULL
);

-- task_state: derived, rebuildable via replay(events)
CREATE TABLE task_state (
  task_id TEXT PRIMARY KEY,
  goal TEXT, success_criteria TEXT, plan TEXT,        -- JSON arrays
  current_subgoal TEXT, completed_subgoals TEXT,      -- JSON array
  current_url TEXT, current_site TEXT,
  recent_actions TEXT,                                -- JSON, sliding window
  blocked_reason TEXT, retry_count INTEGER DEFAULT 0,
  last_checkpoint_step INTEGER
);

-- memories: semantic/episodic facts with provenance
CREATE TABLE memories (
  id INTEGER PRIMARY KEY,
  task_id TEXT, domain TEXT, kind TEXT,               -- 'fact' | 'episodic_summary' | 'skill_ref'
  content TEXT, confidence REAL,
  source_event_id INTEGER, created TEXT, last_verified TEXT,
  embedding BLOB
);
CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='id');
```

**Action schema (GBNF-constrained model output):**
```json
{
  "reason": "Assignment link is visible under Week 3",
  "action": "click",
  "target": 42,
  "expected_result": {"url_contains": "/assignments/", "page_contains": "Recursive Sort"},
  "confidence": 0.9
}
```

**Verification result:**
```json
{"step": 57, "expected": {...}, "actual": {"url": "...", "page_excerpt": "..."}, "result": "fail", "reason": "url unchanged"}
```

---

## 18. Pseudocode

```python
def agent_loop(task_id):
    state = task_state.load_or_replay(task_id)   # crash-safe resume
    while not state.complete:
        raw_page = browser.observe()
        page_hash = hash(raw_page)
        page_repr = delta_engine.reduce(raw_page, state.last_page_hash, state.steps_since_full_refresh)

        memories = memory_router.retrieve(state.current_subgoal, page_repr, domain=state.current_site)

        context = context_builder.build(
            stable_prefix=state.static_block,          # system/task/plan/tool-schema — cache-friendly
            running_summary=state.running_summary,
            recent_window=state.recent_turns,           # raw, small
            memories=memories,
            page=page_repr,
        )

        decision = local_llm(context)                   # GBNF-constrained JSON

        if is_consequential(decision.action) and not approved(decision):
            request_user_approval(decision); continue

        pre_hash = page_hash
        result = executor.execute(decision.action, idempotency_key=(state.current_step, pre_hash))
        new_page = browser.observe()
        verification = verifier.check(decision.expected_result, new_page)

        event_store.append(task_id, state.current_step, decision, result, verification)
        state = task_state.update(state, decision, result, verification)
        checkpoint(state)                                 # fsync

        if loop_detector.repeated(state) or navigation_loop_detector.detect(state):
            state = recovery.escalate(state, level="refresh_state")
        elif verification.result == "fail":
            state = recovery.escalate(state, level=next_level(state.recovery_level))
        else:
            state.recovery_level = "normal"

        if compactor.should_compact(state):
            state.running_summary = compactor.compact(state.running_summary, state.dropped_turns)

        if state.needs_replan:
            state.plan, state.current_subgoal = planner.replan(state)   # low-frequency, can use a larger model
```

---

## 19. Benchmark Plan

Tiered tasks (per the user's original Tier 1–5 design) run against each architecture variant (A–F from Section 5) with identical hardware/model/runtime held constant, varying only the memory/context subsystem. Measure per run: task success (binary + partial-credit on success criteria), total LLM calls, input/output tokens, average and peak context size, prompt-processing (prefill) latency vs. generation latency, VRAM high-water mark, retries, detected loops, incorrect actions, human interventions requested. Run each tier ×5 with different seeds/sites to control for site-specific variance. Compare Qwen3-8B against Ministral-8B/GLM-4-9B on the same harness specifically to close the model-comparison gap flagged in Section 4 (no independent benchmark currently settles this).

---

## 20. Expected Performance

Explicitly separating measured facts from engineering estimates.

**Measured/sourced:** Qwen3-14B Q4_K_M weights ≈ 8.0–8.5GB; general local-LLM VRAM rule of +1–3GB overhead and +1–2GB per 8K–32K context for KV cache (localllm.in); WebVoyager multimodal vs. text-only gap (59.1% vs. 40.1%) — large-model, not directly transferable to 7–14B.

**Engineering estimates (labeled, not sourced):**
- ~800–1500 input tokens/action in normal mode, ~1 LLM call/action for the executor (plus ~1 planner call per 5–15 executor steps).
- Normal-mode context ~3.5–6K tokens; recovery-mode excursions to ~10–16K, expected on the order of 5–15% of steps in a well-tuned system.
- With llama.cpp prefix-caching, expect prefill cost to be dominated by the *volatile* tail (page state + recent turns), not the full context — the static prefix should re-hit cache on nearly every call.
- No reliable published measurement found for local small-model, AX-tree-driven, long-horizon (100+ action) browser-task success rate — this is exactly what Section 19's benchmark is for; do not assume Claude-level completion rates without measuring.

---

## 21. Implementation Roadmap

**Phase 1 — Basic loop:** Qwen3-8B (llama.cpp) + Playwright, compact interactive-element extraction, single-tier context (no compaction yet), constrained JSON action schema. *Exit criteria:* completes Tier 1–2 tasks reliably; JSON output never malformed (GBNF working).

**Phase 2 — Verification + idempotency:** add `expected_result` + Verifier, pre/post-action hashing, recovery ladder (RETRY/REFRESH only). *Exit criteria:* measurable drop in silently-wrong actions on Tier 2–3 tasks.

**Phase 3 — Event-sourced state:** `events` table, `task_state` as derived view, checkpoint/replay-based crash recovery. *Exit criteria:* kill the process mid-task, resume, task completes without corruption.

**Phase 4 — Tiered context + compaction:** running-summary compactor, recent-turn window, prefix-stable ordering; measure KV-cache hit rate. *Exit criteria:* Tier 4 (30–50 action) tasks stay under the "max normal" token budget without losing task coherence.

**Phase 5 — Hybrid retrieval memory:** FTS5 + local embedding (`bge-small`), Memory Router scoped by domain/subgoal. *Exit criteria:* agent reuses a fact/skill learned earlier in the same task without re-discovering it.

**Phase 6 — Page deltas + confidence escalation:** hash-based delta engine with drift guard; AX-tree→screenshot+SoM escalation ladder. *Exit criteria:* Tier 5 (100+ action) task completes with average context size not meaningfully larger than Tier 3.

**Phase 7 — Skills/procedural memory:** per-domain `skills/*.md`, populated manually first, then from successful-trajectory compression. *Exit criteria:* second run of a previously-seen site/task uses fewer LLM calls than the first.

**Phase 8 — Benchmark + model bake-off:** run Section 19's harness across Qwen3-8B/14B vs. Ministral-8B/GLM-4-9B, and across architecture variants A–F, to settle the still-open model-comparison and disposable-vs-tiered-context questions with this project's own data rather than secondary literature.

---

## 22. What NOT to Build

- **A heavyweight vector database (Qdrant/Chroma/pgvector as a separate service).** The corpus size here (hundreds to low thousands of memory rows per agent) doesn't need it — `sqlite-vec` or brute-force cosine over embeddings stored as BLOBs in the same SQLite file is sufficient and keeps the whole memory subsystem in one file.
- **A second LLM as the memory router.** Evidence supports deterministic BM25+embedding retrieval; spending an LLM call to decide what to retrieve is unjustified extra latency/cost for this problem size.
- **Full disposable/stateless context as originally hypothesized.** Falsified in Section 4 — don't build the "clear everything, reload memory.md" loop as the primary mechanism.
- **BrowserOS as the backend.** Immature third-party integration story relative to Playwright/CDP for a custom agent (Section 4/15).
- **vLLM or multi-GPU serving for v1.** Unconfirmed benefit at single 8–12GB-GPU scale; adds real operational complexity for a benefit that isn't evidenced yet.
- **RL fine-tuning the base model before the architecture is validated.** The 2026 papers cited in Section 4 improve Qwen3 via fine-tuning, but that's an optimization on top of a working architecture, not a prerequisite — validate the system with an off-the-shelf model first (Phase 8 before any fine-tuning investment).
- **A generic multi-agent orchestration framework (AutoGen/CrewAI-style) as the backbone.** The task here is a single agent with a planner/executor split, not a multi-agent negotiation problem; adopting a heavier framework than needed adds indirection without evidenced benefit.

---

## 23. Final Recommended Stack

```
MODEL:            Qwen3-8B, Q4_K_M/Q5_K_M GGUF (Qwen3-14B optional upgrade path at 16GB+, or as
                   an occasionally-swapped-in planner model)

INFERENCE:        llama.cpp (native GBNF grammar-constrained JSON, prompt/KV-cache reuse via
                   cache_prompt + n_cache_reuse) — LM Studio acceptable as a GUI wrapper

BROWSER:          Playwright, via Playwright MCP where convenient (accessibility-tree-first,
                   built-in actionability checks); documented escape hatch to raw CDP if
                   per-step latency at high call volume becomes the bottleneck

PAGE OBSERVATION: Compact interactive-element/AX list by default; escalates to full AX tree,
                   then screenshot+Set-of-Mark, only on low confidence or verification failure

WORKING MEMORY:   task_state (derived/materialized view, JSON), rebuilt by replaying the
                   event log — never hand-authored, never the primary persisted artifact

LONG-TERM MEMORY: SQLite — append-only events (source of truth) + memories table
                   (episodic/semantic/procedural) with confidence + provenance columns

RETRIEVAL:        Hybrid: SQLite FTS5 (BM25) + small local embedding (bge-small-en-v1.5,
                   ~130MB, CPU-cheap) via sqlite-vec or brute-force cosine, fused, deterministic
                   router (no second LLM call)

SKILLS:           Markdown per domain (skills/<domain>.md), the one place prose memory is
                   still correct — small, bounded, retrieved only on domain match

STATE STORAGE:    Single SQLite file per task (events + task_state + memories together);
                   fsync on every checkpoint for crash-safety

CONTEXT TARGET:   ~3.5–6K tokens normal mode, ordered stable-prefix-first for KV-cache reuse;
                   10–16K only in recovery mode, not routine

ACTION FORMAT:    GBNF-grammar-constrained JSON: {reason, action, target, params,
                   expected_result, confidence}; small constrained action vocabulary
                   (open_url/click/type/select/scroll/back/extract/download/wait/finish)

RECOVERY:         NORMAL → RETRY → REFRESH STATE → DEEP REASONING → REPLAN → ASK USER,
                   with idempotency keys (pre-action state hash) preventing unsafe retries
                   of consequential actions

VERIFICATION:     Every action carries expected_result; deterministic post-action check
                   against actual browser state before the loop continues — never assume
                   "executed" implies "succeeded"
```

---

## 24. Critical Questions — Answered

1. **Continuous conversation at all?** No, but not fully disposable either — a prefix-stable, selectively-compacted context (Section 6), not a raw growing chat transcript and not a full reset.
2. **Clear context after every action / every few / occasionally?** Never fully clear. Compact the *middle* (older turns → running summary) when a token threshold is hit; keep the stable prefix and recent-turn window intact always.
3. **Should `memory.md` exist?** Only for procedural skills (Section 8) — not as the primary working/episodic memory mechanism, which is falsified in Section 2/4.
4. **If Markdown isn't primary, what replaces it?** Event-sourced SQLite: append-only `events` (truth) + derived `task_state` (JSON) + `memories` (hybrid-retrieved facts/summaries with provenance).
5. **Should SQLite be used?** Yes — single-file, holds events/state/memories together, supports FTS5 natively, and is what OpenHands's event-sourcing pattern and this project's crash-recovery model both need.
6. **Do we need embeddings/vector search?** Yes, alongside BM25, not instead of it — falsified the BM25-alone hypothesis (Section 4); a small local embedding model is cheap enough not to matter at this VRAM budget.
7. **Should screenshots normally be sent?** No — compact AX/interactive-element extraction is the default; screenshots are an escalation path, not routine.
8. **What instead of screenshots?** Reduced accessibility-tree/interactive-element lists (Section 9), escalating to full AX tree, then screenshot+SoM only when confidence is low or verification fails.
9. **Entire accessibility tree or reduced?** Reduced by default (compact interactive-element list); full AX tree is the first escalation step, not the default.
10. **Should page deltas be used?** Yes, with guardrails: same-page-small-change → delta; navigation or unexpected result → full refresh; forced full refresh every ~20 actions regardless, to bound drift.
11. **How often should a full page representation regenerate?** On navigation, on verification failure, and at a fixed step interval (~20) as a drift guard — not on a fixed timer otherwise.
12. **What should always remain in working context?** Goal, success criteria, current plan/subgoal, tool schema, recent-turn window, current page state.
13. **What should never normally be in working context?** Full raw DOM/HTML, the full event log, the full skills library, or memories outside the current subgoal's retrieval scope.
14. **Tokens per normal inference?** ~3.5–6K (Section 10) — an engineering target, to be measured against actual benchmark runs (Section 19).
15. **Maximum ordinary working context?** ~6K; beyond that the system should be in recovery mode, not normal mode.
16. **Long chain-of-thought every action?** No — short `reason` field per action by default; deeper reasoning reserved for recovery mode (Section 12), matching the FAST/RECOVERY split the user originally proposed.
17. **Multiple actions per inference?** Yes, for deterministic, low-risk batches specifically (e.g., filling multiple known form fields at once via a `fill_form`-style batched action) — not for anything consequential or uncertain, where single-step-then-verify is safer.
18. **Should repeated workflows become deterministic macros?** Yes — Phase 7's procedural-skill compression is explicitly designed to reduce LLM involvement on previously-solved domains over time.
19. **Browser backend?** Playwright (via Playwright MCP), with a CDP escape hatch (Section 15) — not BrowserOS.
20. **Local model?** Qwen3-8B primary, Qwen3-14B as an upgrade/planner option (Section 13) — acknowledged as the best-*supported*, not proven-superior, choice given verifiable evidence.
21. **Inference engine?** llama.cpp (Section 14), for GBNF grammar support and working KV-cache reuse.
22. **How should failures be detected?** Deterministically: hash-based no-op/loop/navigation-loop detectors, `expected_result` mismatches, stale target ids, modal-not-handled checks (Section 12) — never left to the LLM to self-report.
23. **How should the agent recover after losing track of the browser?** Recovery ladder (Section 12): retry → force full state refresh → deeper reasoning with escalated observation → replan → ask user, gated by the failure detectors above.
24. **How should the agent resume after a full program crash?** Reload `task_state` (or replay `events` if the materialized view is stale/missing) from the last fsynced checkpoint, re-observe the browser fresh (session/cookies permitting), and continue from `current_subgoal` (Section 12/17).
25. **What architecture gives the best realistic chance of approaching Claude-like usability on a much smaller model?** The one in Section 6/23 — because it's the only variant, of those compared in Section 5, that matches what every examined system actually does at long horizons (event-sourced state, tiered/compacted context, hybrid retrieval, hierarchical planning, mandatory verification) rather than a single plausible-sounding simplification of any one of those pieces.

---

*Sources are cited inline by arXiv ID / repository / documentation URL throughout Sections 2–4; where no reliable published measurement could be found, this is stated explicitly rather than estimated silently (Section 20).*
