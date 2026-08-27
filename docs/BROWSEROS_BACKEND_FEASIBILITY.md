# BrowserOS Backend Feasibility Study

Date: 2026-08-27
Branch: `research/browseros-backend-feasibility` (not merged; research-only, per instruction)
Baseline: `main` @ tag `phase5b-pass`
Author: architecture research pass, no production code changed except a documentation-only file plus one throwaway experiment note (Section 20)

This is a feasibility study, not an implementation. No BrowserAgent source file is modified by
this branch. The one prior architectural pass on this exact question — `ARCHITECTURE.md`
Section 4 ("Browser backend: Playwright vs. alternatives") and Section 15 ("Browser Backend
Recommendation") — already evaluated and rejected BrowserOS once, before Phase 1 was built. This
study re-examines that call against the *current* BrowserOS (as of 2026-08-27) and against the
*actual* BrowserAgent codebase as it exists today (post Phase 5B), not a hypothetical one.

---

## 1. Executive Verdict

**USE_PLAYWRIGHT_CDP_ATTACH** — not `STAY_WITH_PLAYWRIGHT` unchanged, not `ADD_BROWSEROS_BACKEND`,
not `REPLACE_PLAYWRIGHT_WITH_BROWSEROS`.

The real thing worth wanting out of this proposal is stated precisely in the prompt's own
closing principle: separating *browser lifetime* from *BrowserAgent process lifetime*. That is
a genuine, valuable idea — per-task browser relaunch is real, measurable overhead, and reusing
an already-logged-in browser instead of a fresh profile-restore is a real UX improvement for
authenticated sites.

But the specific vehicle proposed — BrowserOS — does **not** demonstrate the one property that
would justify adopting it: **live session persistence across an automating client disconnecting
and reconnecting later.** BrowserOS's own documentation for its flagship agent product (BrowserOS
neo) states the opposite: *"Close an AI's session and its tab group closes with it. The tabs it
opened go away in one step."* (Section 4, Section 6 below — primary source cited). No page in
BrowserOS's documentation set confirms live-tab survival across an MCP client disconnect/reconnect
for plain BrowserOS either; it is simply undocumented, not confirmed.

Meanwhile, Playwright — the backend BrowserAgent already has, with zero new dependency, zero new
network-exposed control surface, zero license entanglement (AGPL-3.0 vs. none) — already ships a
first-party, primary-source-verified method for exactly this: `BrowserType.connect_over_cdp()`,
confirmed present and documented in the installed `playwright` package on this machine (Section 17,
Section 19). Attaching Playwright to a long-running, user-launched, already-authenticated Chromium
process gives BrowserAgent the actual thing it wants — a browser whose lifetime the user controls,
not BrowserAgent — while leaving `PlaywrightBackend`, `PageObservation`, numeric target IDs,
the verifier, event sourcing, crash recovery, and every test fixture **completely unchanged**,
because `connect_over_cdp()` returns the exact same `Browser`/`Page`/`Locator` types Playwright's
`launch_persistent_context()` already returns.

This is the FINAL PRINCIPLE's own fallback, taken seriously: *"If Playwright attached to
persistent Chromium via CDP gives us 90% of the benefit with much less dependency risk, say that
instead."* The evidence says that.

The two router bugs (Findings 7 and 8) that motivated part of this inquiry are **fully
backend-independent**, confirmed by direct code reading (Section 4/23): switching to BrowserOS
would not fix either one.

---

## 2. BrowserAgent Current Architecture

Read directly (not summarized from memory) for this study: `README.md`, `ARCHITECTURE.md`,
`docs/PHASE4B_REPORT.md`, `docs/PHASE5_REPORT.md`, `docs/PHASE5B_REPORT.md`, and the source under
`agent/`, `browser/`, `batch/`, `inference/`, `memory/`, `router/`, `ui/`, `workflow/`.

Pipeline, as implemented (not aspirational):

```
Qwen3-8B (Ollama, RTX 4070)
   ↑ prompt: rendered PageObservation + task/memory context (agent/context or equivalent)
   ↓ ModelDecision (Pydantic-schema-constrained JSON: action, target, params, expected_result)
agent/decision.py    — parses/validates the raw model JSON into ModelDecision
agent/schemas.py     — classify_risk() : ActionType, element_name(str) -> RiskLevel
agent/loop.py::step  — orchestrates: observe -> decide -> risk-gate -> approval -> execute -> re-observe -> verify -> event-log
self.browser.<method>() — the ONLY way agent/loop.py touches a browser (see Section 3)
browser/playwright_backend.py::PlaywrightBackend — the one module allowed to touch a live Playwright Page
browser/observer.py::extract_observation — one page.evaluate() JS pass -> PageObservation
browser/page_model.py::PageObservation/ElementRef — pure Pydantic models, numeric target IDs owned by BrowserAgent
agent/verifier.py (referenced via verifier_mod in agent/loop.py) — before/after PageObservation diff
memory/event_store.py — append-only EventType log (ACTION_INTENT, ACTION_RESULT, VERIFICATION_RESULT, ...)
memory/task_state.py — derived/materialized TaskState replayed from the event log
```

Higher layers reuse this same `AgentLoop` engine rather than re-implementing it:
`batch/orchestrator.py::AgentLoopChildRunner` and `workflow/orchestrator.py::WorkflowOrchestrator`
both construct an `AgentLoop` per work item/step and drive it the same way a single CLI task would
— confirmed by reading both files; neither imports `playwright` (grep, Section 3).

---

## 3. Current Playwright Coupling — Dependency Map

Exhaustive grep of the project source (excluding `.venv/`) for `playwright`/`Playwright`:

```
agent/decision.py               — one comment only ("...as something that reaches Playwright"), no import
agent/loop.py                   — imports PlaywrightBackend, constructs it once (__init__), calls only
                                   abstract methods afterward
browser/observer.py             — docstring mentions Playwright; the function itself takes `page: Any`
                                   and calls exactly one method on it: page.evaluate(js, args)
browser/page_model.py           — comment only, explaining why IDs are NOT cached Playwright handles
browser/playwright_backend.py   — the concrete backend; imports playwright.async_api directly (by design —
                                   this is the intended seam)
research/discovery.py           — imports PlaywrightBackend, constructs it once, calls only
                                   start/open_url/observe/close
tests/integration/*.py,
tests/unit/test_element_mapping.py,
tests/unit/test_observation.py  — use a fake/stub Page object satisfying the same evaluate() contract,
                                   not real Playwright, for the unit-level tests; the integration tests
                                   under tests/integration/test_phase1_browser_actions.py and
                                   test_phase3_crash_recovery.py do launch real Playwright (this is the
                                   deliberate "PlaywrightBackend is real, everything above it is faked"
                                   split — see Section 11)
```

Answers to the 10 required questions, each traced to the exact line(s):

1. **Which components directly import Playwright?** Exactly two application modules construct
   `PlaywrightBackend`: `agent/loop.py:43` (import) / `:86` (construct), and
   `research/discovery.py:24` / `:181`. `browser/playwright_backend.py` itself, by design, imports
   `playwright.async_api` — it is the intended, sole Playwright-touching module (its own docstring:
   *"the only module allowed to touch a live `Page`"*).
2. **Which components depend only on BrowserAgent abstractions?** Everything else: `agent/loop.py`'s
   *usage* of `self.browser` (12 method calls: `start, close, open_url, observe, click, type, select,
   scroll, back, extract, download, wait` — grepped exhaustively, Section 3 above), `agent/verifier.py`
   equivalent, `agent/auth_detect.py` (operates purely on `PageObservation`, zero browser import),
   `batch/orchestrator.py`, `workflow/orchestrator.py`, `router/*`, `ui/*`, `memory/*` — none import
   `playwright`.
3. **Do browser/page/context objects leak into higher layers?** No. `agent/loop.py` never accesses
   `self.browser.page`, `.context`, or any Playwright type — confirmed by grep (`self\.browser\.` matches
   only the 12 abstract methods listed above, never `.page`/`.context`). `PlaywrightBackend._resolve()`
   builds a `Locator` internally but returns it to no one; every public method returns a plain value
   (`None`, `str`, `PageObservation`, or a `dict`).
4. **Is `PageObservation` Playwright-specific?** No. `browser/page_model.py`'s `PageObservation` and
   `ElementRef` are pure Pydantic `BaseModel`s — `url: str`, `title: str`, `elements: list[ElementRef]`,
   `visible_text: list[str]`, etc. No Playwright type appears anywhere in the class definitions.
5. **Do target IDs depend on Playwright DOM extraction?** No, by explicit design. `page_model.py`'s
   own docstring: *"IDs are reassigned fresh on every observation from a positional (selector,
   nth-index) pair — never a cached Playwright handle, which would go stale the moment the DOM
   re-renders."* IDs are BrowserAgent-owned integers (`browser/observer.py:178`, `id=i` from
   `enumerate(..., start=1)`), paired with a `SelectorHint(css, nth)` that is re-resolved fresh on
   every action. `SelectorHint.css`/`.nth` is a CSS selector + positional index — a concept every
   CDP-capable backend can express, not a Playwright-only construct.
6. **Does verification depend on Playwright?** No — traced through `agent/loop.py`: verification
   calls `verifier_mod.check(ExpectedResult(...), observation)` where `observation` is a
   `PageObservation`. The verifier only ever compares two `PageObservation` snapshots (URL, text,
   element state, `state_hash`) — it never touches a live Playwright object.
7. **Do downloads depend on Playwright?** Yes, in the current implementation.
   `playwright_backend.py::download()` uses `self.page.expect_download()`, a Playwright-specific
   async context manager with no generic equivalent in the `BrowserBackend` surface today — this is
   real, backend-specific code that a new backend would have to reimplement against whatever download
   primitive it exposes (see Section 9 action-mapping table).
8. **Does crash recovery assume Playwright lifecycle semantics?** Yes, but as a documented
   **implementation choice**, not a Playwright limitation. `agent/loop.py::_reconcile_pending_intent`'s
   own comment: *"A fresh persistent-context browser starts on a blank page — cookies/storage persist
   across a restart, but the previously-open tab and its live DOM do not."* The reconciliation logic
   (re-navigate to the last-known URL, re-observe, diff against `expected_result`, treat an
   unconfirmed CONSEQUENTIAL action as blocked-pending-human-review) is written to be correct *without*
   assuming a live tab survives — this is not a gap forced by Playwright, it is a deliberate
   design that happens to also work if a live tab is unavailable. See Section 11 for what changes,
   if anything, if live-tab persistence become available.
9. **Do persistent profiles assume Playwright?** No — the *mechanism* (`launch_persistent_context(user_data_dir=...)`)
   is Playwright's API, but the *concept* (a Chromium user-data-dir on disk holding cookies/
   localStorage/IndexedDB across process restarts) is a Chromium property, not a Playwright one.
   Any Chromium-based backend, including BrowserOS, provides the same property by the same underlying
   mechanism (a Chromium profile directory).
10. **Do tests/fixtures fundamentally require Playwright?** Partially. Unit tests
    (`tests/unit/test_element_mapping.py`, `tests/unit/test_observation.py`) use a hand-written fake
    object satisfying `evaluate()` — no real Playwright needed. Integration tests
    (`tests/integration/test_phase1_browser_actions.py`, `test_phase3_crash_recovery.py`) do launch
    real Playwright/Chromium against local HTML fixtures (`tests/fixtures/simple_site/*.html`) — these
    fixtures themselves (plain HTML/JS) are backend-agnostic; only the test *driver* is Playwright-specific,
    and it is isolated to those two files plus the benchmark scripts under `benchmarks/`.

**Overall coupling verdict:** BrowserAgent already has close to a textbook `BrowserBackend` seam —
this was evidently a deliberate design goal from Phase 1 (the "never a cached Playwright handle"
comments in `page_model.py`/`observer.py` predate this study). A formal `BrowserBackend` ABC does
not exist as a named class today, but the *de facto* interface used by `agent/loop.py` and
`research/discovery.py` is already exactly the 12-method surface both call sites use. Introducing
a literal `class BrowserBackend(Protocol)` would be close to a no-op refactor (Section 12).

---

## 4. Current Bugs: Backend Attribution

Per-item classification (`PLAYWRIGHT_CAUSED` / `PLAYWRIGHT_CONTRIBUTING` / `BACKEND_INDEPENDENT` / `UNKNOWN`):

| Problem | Classification | Evidence |
|---|---|---|
| **Finding 7** — sequential-workflow step decomposition reuses the full raw prompt as every step's objective | **BACKEND_INDEPENDENT** | `router/extract.py:91-104` — `try_deterministic_route()`'s sequential branch: `WorkflowStepPlan(ordinal=i+1, target=url, objective=stripped)` reuses `stripped` (the whole prompt) for every step. This runs entirely on the raw prompt string, before any browser object exists. Verified live in this session (Section 23). |
| **Finding 8** — `_ACTION_VERBS` missing enter/type/fill, misroutes find-then-enter prompts to `multisite_sweep` | **BACKEND_INDEPENDENT** | `router/extract.py:20-23` — the `_ACTION_VERBS` regex literal: `change\|set\|toggle\|select\|enable\|disable\|update\|configure\|switch\|turn on\|turn off`. No `enter`, `type`, or `fill`. Pure regex over a string; no browser call anywhere near this code. Verified live in this session (Section 23). |
| Windows pytest/Playwright launch stalls | **PLAYWRIGHT_CONTRIBUTING** | `docs/PHASE5B_REPORT.md` Section 12: a single combined `pytest` invocation intermittently stalls partway through after many cumulative live-model/Playwright sessions in one process; the report's own diagnosis is that `PlaywrightBackend.start()`/`launch_persistent_context()` called directly (outside pytest) always completes in <1s even while the stall reproduces inside pytest — i.e. Windows/pytest-asyncio resource accumulation across a long single process, not a Playwright defect per se. Contributing because the specific resource being exhausted is a Playwright/Chromium subprocess handle; a different backend's own connection/session object could exhibit an analogous but not identical failure mode — no evidence either way, so this is not classified PLAYWRIGHT_CAUSED. |
| Agent owns the browser process 1:1 with task lifetime | **PLAYWRIGHT_CONTRIBUTING** (implementation choice, not a Playwright limit) | `agent/loop.py:131,134` — `await self.browser.start()` / `await self.browser.close()` bracket every task. Playwright itself supports attaching to an already-running browser (`connect_over_cdp`, Section 17) — nothing in Playwright forces per-task ownership; this is how `AgentLoop` currently chooses to use it. |
| Persistent authenticated sessions (cookies/storage) | **BACKEND_INDEPENDENT** | Both Playwright (`launch_persistent_context(user_data_dir=...)`) and BrowserOS (a Chromium profile directory) provide this via the same underlying Chromium mechanism (Section 3, Q9). Not a differentiator. |
| Crash/reconnect (live-tab) behavior | **PLAYWRIGHT_CONTRIBUTING** (implementation choice) | Section 3, Q8. The reconciliation logic assumes the tab does not survive; this is written that way on purpose, not because Playwright cannot preserve a live tab (it can, via CDP-attach, Section 11). |
| Manual login handoff | **BACKEND_INDEPENDENT** | `agent/auth_detect.py` operates entirely on `PageObservation` (title/visible_text/element.sensitive) — zero Playwright import, zero browser-specific logic. |
| Page observation (schema/model) | **BACKEND_INDEPENDENT** | `PageObservation`/`ElementRef` are pure Pydantic (Section 3, Q4). |
| Page observation (extraction implementation) | **PLAYWRIGHT_CONTRIBUTING** | `browser/observer.py::extract_observation` is wired to a Playwright `Page.evaluate()` call today, though the function signature (`page: Any`) is duck-typed and only needs one method. |
| Element targeting (design) | **BACKEND_INDEPENDENT** | Numeric IDs + `SelectorHint(css, nth)`, never a cached handle (Section 3, Q5). |
| Element targeting (implementation) | **PLAYWRIGHT_CONTRIBUTING** | `PlaywrightBackend._resolve()` builds a Playwright `Locator` specifically. |
| Downloads | **PLAYWRIGHT_CONTRIBUTING** | `page.expect_download()` is Playwright-specific API (Section 3, Q7). |
| New tabs / multi-tab | **BACKEND_INDEPENDENT** | Current `PlaywrightBackend` only tracks `self.page` (one page) — this is a BrowserAgent scope gap, not a Playwright limitation; Playwright fully supports `context.pages`/`new_page()` today, unused by BrowserAgent so far. |
| Popups / modals | **BACKEND_INDEPENDENT gap, both backends** | `PageObservation.modal_present` (DOM `[role="dialog"]` detection) is backend-agnostic and already implemented. Native `window.alert/confirm/prompt` dialogs are not specially handled in any file read this study — this gap is identical regardless of backend; both Playwright and BrowserOS would need an explicit dialog handler added. |
| Browser startup cost (fresh launch per task) | **PLAYWRIGHT_CONTRIBUTING** (implementation choice, real cost) | Chromium cold-launch is genuinely not free; this is the one item on this list where "keep the browser running independent of the agent process" is a legitimate, evidence-backed win — see Section 11/15 for how CDP-attach captures this same win without BrowserOS. |
| Real-site compatibility | **UNKNOWN** | Neither backend has enough real-site trial volume in this repo's evidence (Phase 5/5B's own stated limitation) to support a claim either way; switching backends does not resolve this by itself. |
| Long-running browser state surviving agent exit | **UNKNOWN, and the specific claim under review here is contradicted, not supported, by BrowserOS's own docs** | Section 6 — BrowserOS neo's documentation states AI-session tabs close when the session ends. |
| Browser closes when BrowserAgent process ends | **PLAYWRIGHT_CONTRIBUTING** (implementation choice) | `agent/loop.py:134` always calls `self.browser.close()`. Nothing in Playwright forces this; a CDP-attach mode could simply not call `browser.close()` (only `browser.disconnect it via context manager exit) and leave the launcher-owned Chromium running. |

---

## 5. BrowserOS Architecture (Primary Sources)

Primary sources used, all fetched live during this study (2026-08-27):

- Repository: [github.com/browseros-ai/BrowserOS](https://github.com/browseros-ai/BrowserOS)
- GitHub API metadata: `api.github.com/repos/browseros-ai/BrowserOS`
- Documentation index: [docs.browseros.com/llms.txt](https://docs.browseros.com/llms.txt)
- [docs.browseros.com/neo/mcp/manual.md](https://docs.browseros.com/neo/mcp/manual.md) — generic MCP client connection
- [docs.browseros.com/features/use-with-claude-code.md](https://docs.browseros.com/features/use-with-claude-code.md) — MCP server details, tool count
- [docs.browseros.com/neo/tabs-and-isolation.md](https://docs.browseros.com/neo/tabs-and-isolation.md) — tab lifecycle
- [docs.browseros.com/comparisons/chrome-devtools-mcp.md](https://docs.browseros.com/comparisons/chrome-devtools-mcp.md) — observation model
- [docs.browseros.com/troubleshooting/connection-issues.md](https://docs.browseros.com/troubleshooting/connection-issues.md) — restart/reconnect guidance

**What BrowserOS is (as of this study):** an AGPL-3.0, Chromium-based fork (`packages/browseros/`
contains "Chromium fork: patches, build system, signing," sourced in part from ungoogled-chromium
per the repo README) shipping **two distinct products**:

- **BrowserOS** — a daily-driver replacement browser with an AI agent built into every new tab,
  and its own embedded MCP server (so *it* is the thing your external tools connect to).
- **BrowserOS neo** — a second, separate Chromium instance specifically for AI agents to drive,
  with its own tab-group isolation from the user's normal browsing, session replay, and a
  dashboard. This is the product most of the detailed persistence documentation covers.

Answering the 37 numbered questions:

1. **What exactly is BrowserOS today?** Two Chromium-based products (above), both AGPL-3.0,
   maintained by `browseros-ai` (Felafax, Inc.), org repo count 14+.
2. **Chromium-based?** Yes, confirmed — repo contains a Chromium-fork build package.
3. **How does automation work?** MCP (Streamable HTTP transport) is the primary/documented path;
   the codebase also contains CDP-protocol bindings and a CLI.
4. **MCP?** Yes — both products; `neo` exposes `http://127.0.0.1:9200/mcp` (per
   `neo/mcp/manual.md`); plain BrowserOS exposes its own local MCP endpoint reachable via
   `chrome://browseros/mcp` in-browser settings (per `use-with-claude-code.md`, which cites port
   `9239`); the troubleshooting doc separately references port `9100`. **These three port numbers
   across three different official doc pages do not agree with each other** — documentation
   inconsistency, noted per this study's instruction to flag disagreement rather than pick one.
5. **HTTP?** Yes — MCP-over-Streamable-HTTP is itself an HTTP-based transport.
6. **CDP?** The repo contains `cdp-protocol` bindings; not confirmed whether raw CDP is exposed as
   a *separate*, documented external control channel independent of the MCP layer — no doc page in
   the fetched index describes a raw-CDP debugging port for BrowserOS the way Chrome's own
   `--remote-debugging-port` works.
7. **Can an external Python process control it?** Yes, in principle — Streamable HTTP MCP is a
   documented, protocol-standard transport; any client implementing that protocol (a Python `mcp`
   SDK client, or raw HTTP request construction) can speak to it. No BrowserOS-specific Python SDK
   was found documented; the only code example in the fetched docs was JavaScript (Vercel AI SDK)
   and a Go-based CLI (`openclaw`).
8. **Can BrowserAgent connect without Claude?** Yes — the manual-connection doc is explicit that
   *"any client that supports Streamable HTTP works,"* not just Claude-branded tools.
9. **Can local Qwen indirectly control it through our own adapter?** Architecturally yes — Qwen
   never needs to speak MCP directly; a `BrowserOSBackend` adapter (Section 8) would translate
   BrowserAgent's existing `ModelDecision` actions into MCP tool calls, the same way
   `PlaywrightBackend` translates them into Playwright API calls today.
10. **Does BrowserOS itself require a cloud model?** No — "Bring your own AI" (11+ providers) or
    fully local via Ollama/LM Studio, per the repo README and `features/local-models.md`.
11. **Can it be used entirely locally for browser execution?** Yes for the browser-execution layer
    itself (it's a local Chromium process either way); the *agent brain inside BrowserOS's own
    built-in agent* can be configured local-only, but that built-in agent is irrelevant to this
    study — BrowserAgent would use BrowserOS purely as a controllable browser process via MCP, with
    Qwen staying the only decision-making model, unchanged.
12. **Preserve browser profiles?** Yes (Chromium profile directory) — same mechanism Playwright
    already uses (Section 3, Q9).
13-16. **Cookies / localStorage / IndexedDB / authenticated sessions?** All are properties of a
    Chromium profile directory, not BrowserOS-specific — expected to persist by the same mechanism
    Playwright's `launch_persistent_context` already gives BrowserAgent today. No BrowserOS-specific
    documentation was found making a *stronger* persistence claim than "it's a normal Chromium
    profile."
17-18. **Open tabs / tab history?** Documented tab-group isolation (`neo/tabs-and-isolation.md`);
    tab *history* (browser back/forward) not separately documented as an MCP-exposed primitive
    beyond the "History (4): search and manage browsing history" tool category, which reads like
    the bookmarks/history *feature*, not `page.go_back()` semantics — needs verification against an
    actual tool schema, not confirmed from docs alone.
19. **Downloads?** Yes — "File & Export (3): PDF/screenshot saving, file downloads" tool category
    (`use-with-claude-code.md`).
20. **Multiple tabs/windows?** Yes — "Window Management (5)" and "Tab Groups (5)" tool categories
    are explicitly listed.
21. **Popups?** Not separately documented; native JS-dialog handling not confirmed either way.
22. **iframes?** Not addressed in any fetched doc page.
23. **Shadow DOM?** Not addressed in any fetched doc page.
24. **File uploads?** Not explicitly listed among the 53 tools' category breakdown found; "File &
    Export" appears output-oriented (downloads/PDF/screenshot), not confirmed to include upload.
25. **Accessibility tree / DOM representations?** Yes, and richer than the comparison target —
    `take_snapshot` (AX tree), `take_enhanced_snapshot` (BrowserOS-specific), `get_page_content`
    (Markdown), `get_dom` (raw DOM), `get_page_links` — per
    `comparisons/chrome-devtools-mcp.md`.
26. **Screenshots?** Yes, explicitly listed.
27. **Page text?** Yes — `get_page_content` (Markdown extraction).
28. **Element references?** `search_dom` (by text/CSS/XPath) is documented; a snapshot-scoped `ref`
    concept (the pattern used by Playwright MCP/chrome-devtools-mcp/browserbase-mcp) is implied by
    the family of tools but **not explicitly documented for BrowserOS** in any fetched page.
29. **Stable element IDs across calls?** **Not documented either way.** This is a genuine evidence
    gap, and a materially important one for this study (Section 8). As circumstantial evidence
    (not proof) that this is a live pain point in this exact tool family: a GitHub issue on a
    sibling MCP browser-automation server surfaced during this research —
    ["Can't obtain elements' `ref` from snapshot" (browserbase/mcp-server-browserbase#90)]
    (https://github.com/browserbase/mcp-server-browserbase/issues/90) — confirming that ref
    instability/availability is a known category of problem in this family of tools generally, not
    a solved one. This is not evidence about BrowserOS specifically; it is evidence that the
    general pattern BrowserOS follows has a documented history of exactly this kind of problem
    elsewhere, and BrowserOS's own docs are silent on whether it avoids it.
30. **Action primitives?** Yes — 53 tools across navigation/tabs, content/observation,
    interaction/input (14 — click/type/fill/scroll/drag-and-drop), files/export, windows, tab
    groups, bookmarks, history (`use-with-claude-code.md`).
31. **Navigation events?** Implied by the navigation/tabs tool category; no explicit event-stream/
    webhook mechanism documented.
32. **Download events?** Not documented as a push/event mechanism (tools appear to be
    request/response, not an event stream).
33. **Tab events?** Same — no documented event-push mechanism found.
34. **Reconnection after client crash?** **Not documented.**
    `troubleshooting/connection-issues.md` describes manual recovery steps (restart the MCP
    server via Settings, kill the agent process, check port/firewall) — it does not describe
    an automatic reconnect-and-resume-session flow.
35. **Reconnection after BrowserOS restart?** Same page: manual restart procedure documented;
    automatic session continuity after a *BrowserOS-side* restart is not claimed.
36. **Windows support?** Yes — `neo/install.md`: macOS and Windows for neo; plain BrowserOS:
    macOS, Windows, and Linux.
37. **macOS support?** Yes, both products.

**Where documentation and source appear to disagree:** the MCP port number (9200 vs. 9239 vs.
9100) across three different official doc pages, noted above (Q4). This should be independently
re-verified against the live product before any implementation decision, not treated as settled
by any one of these three pages alone.

---

## 6. BrowserOS Persistence Semantics — The Central Question

This is the single most decision-relevant section, per the study's own framing.

**Three tiers, kept explicitly distinct as instructed:**

| Tier | Definition | BrowserOS evidence | BrowserAgent + Playwright today |
|---|---|---|---|
| **Profile persistence** | Cookies, localStorage, IndexedDB, extensions on disk, surviving a full process restart | Confirmed — ordinary Chromium profile directory mechanism (Section 5, Q12-16) | **Already have this.** `launch_persistent_context(user_data_dir=...)` (`browser/playwright_backend.py:40-45`), one directory per task under `tasks_dir/<task_id>/browser_profile`. |
| **Live tab persistence** | The actual open tab / in-page JS state / DOM continues existing, unmodified, across the *automating client* disconnecting and later reconnecting | **Contradicted for BrowserOS neo's own agent-session tabs**: *"Close an AI's session and its tab group closes with it. The tabs it opened go away in one step."* (`neo/tabs-and-isolation.md`). **Undocumented, not confirmed, for plain BrowserOS's ordinary tabs** — no fetched page states that a tab opened via the local MCP server continues to exist, untouched, after an MCP client disconnects and later reconnects. | **Explicitly does not have this today, by design** — `agent/loop.py`'s own comment: cookies/storage persist across a restart, "the previously-open tab and its live DOM do not." Recovery is written around this fact (Section 3, Q8), not blocked by its absence. |
| **Automation session persistence** | The MCP/control server itself keeps running and remains addressable, independent of any one client connection, as long as the browser application process is alive | Plausible in principle (local HTTP server bound to the browser process) but **no documented reconnect/resume semantics**; troubleshooting doc describes only manual restart recovery (Section 5, Q34-35). | N/A — Playwright's persistent-context model does not have a standing server to reconnect to; each `AgentLoop` run owns its own Playwright connection for its own lifetime. CDP-attach (Section 11) would give BrowserAgent an equivalent of this tier, on top of a browser process the *user* — not BrowserAgent — launches and keeps running. |

**Direct answer to the architecture sketched in the prompt** (*BrowserOS runs independently →
I log in manually → BrowserOS retains my profile → BrowserAgent connects → performs task → exits
→ BrowserOS stays open → tabs/session remain → BrowserAgent reconnects later*):

- **"BrowserOS retains my profile"** — supported (profile-persistence tier).
- **"BrowserOS stays open"** — supported; that's just leaving the application running, independent
  of anything BrowserAgent does.
- **"tabs/session remain"** — **not supported by primary sources for the agent-driven tab
  specifically.** BrowserOS neo's own documentation says the opposite for its AI-session tabs. For
  plain BrowserOS, this is simply unaddressed in the documentation set fetched — silence, not
  confirmation.

This is the load-bearing finding of this study: the one property that would most justify choosing
BrowserOS over "attach Playwright to a long-running Chromium" — surviving an agent disconnect with
the *same live tab* still there — is not demonstrated to exist, and is directly contradicted for
the product's flagship agent-facing offering.

---

## 7. BrowserOS API/MCP Capabilities Summary

(Consolidated from Section 5.) 53 tools across 8 categories: navigation/tabs (8), content/
observation (8, including AX snapshot + enhanced snapshot + Markdown + raw DOM + links), interaction/
input (14, including click/type/fill/scroll/drag-and-drop), files/export (3), windows (5), tab
groups (5), bookmarks (6), history (4). Transport: MCP over Streamable HTTP, localhost-bound, no
authentication documented (the local bind is the implicit trust boundary — see Section 12).

---

## 8. Observation Compatibility

BrowserAgent's `PageObservation` needs, at minimum, per action cycle: `url`, `title`, a bounded
list of interactive elements with `role`/`name`/`value`/`disabled`/`selected`/`checked`/`options`,
a bounded list of visible text, a modal-present flag, and a `state_hash` for change detection —
plus, critically, a way to **re-resolve** a previously-assigned numeric target ID against the
*current* live DOM right before acting (never a cached handle — Section 3, Q5).

BrowserOS's `take_snapshot`/`take_enhanced_snapshot` tools are plausible sources for the
element/role/name portion; `get_page_content` (Markdown) or `get_dom` could supplement or replace
the visible-text extraction. **The open question this study could not resolve from documentation
alone is whether BrowserOS's own element identifiers are stable and re-resolvable in the same way
BrowserAgent's `SelectorHint(css, nth)` is** — i.e., whether a `ref` returned by one `take_snapshot`
call is guaranteed valid, or re-derivable, for a `click`/`fill` call made moments later after the
DOM has re-rendered. No BrowserOS doc page addresses this directly (Section 5, Q28-29), and the
one piece of circumstantial evidence found (a sibling MCP tool's open GitHub issue about exactly
this failure mode) argues for treating it as unproven rather than assuming it away.

**If a `BrowserOSBackend` were ever built, the correct design is *not* to change
`PageObservation`.** Keep `PageObservation`/`ElementRef` exactly as they are (they are
already backend-agnostic Pydantic models — Section 3, Q4) and have `BrowserOSBackend.observe()`
call `take_snapshot`/`take_enhanced_snapshot`, translate the response into BrowserAgent's own
freshly-assigned numeric IDs (the same "renumber from position on every observation" pattern
`observer.py` already uses), and internally maintain a private mapping from
`BrowserAgent target N -> BrowserOS ref` scoped to that one observation only — mirroring exactly
how `SelectorHint` is scoped to one observation today. Qwen would see identical prompt text either
way; this is entirely containable inside the adapter and would require zero changes to the model,
the prompt format, or any test that operates above `PlaywrightBackend`.

If BrowserOS refs prove unstable between the `take_snapshot` call and the subsequent action call
(the exact failure mode flagged as unresolved above), the adapter would need to re-run
`search_dom`/re-snapshot immediately before every action rather than trusting a stored ref — adding
real latency and a new failure mode not present in the Playwright implementation, where
`selector_hint.css` + `nth` is cheap to re-resolve because it's a plain CSS query, not a
server-tracked reference.

---

## 9. Action Compatibility — Mapping Table

| BrowserAgent action | Current Playwright implementation | BrowserOS MCP equivalent (documented category) | Semantic mismatch | Implementation difficulty |
|---|---|---|---|---|
| `open_url` | `page.goto(url, wait_until="domcontentloaded")` | Navigation/tabs tool | Low — both are a plain navigate-and-wait | Low |
| `click` | `locator.click()` after `_resolve()` | Interaction/input tool, needs an element ref | Ref-stability risk (Section 8) — otherwise low | Medium |
| `type` | `locator.fill(text)` | Interaction/input tool (`fill`) | Low, same caveat as click | Medium |
| `select` | `locator.select_option(label=value)` | Not confirmed as a distinct dropdown-select primitive in fetched docs — may need to be simulated via click+type or a documented select tool not found in this pass | Real, unresolved | Medium-High |
| `scroll` | `page.mouse.wheel(0, delta)` | Interaction/input tool (scroll listed) | Low | Low |
| `back` | `page.go_back(wait_until="domcontentloaded")` | History tool category — semantics (session-history back vs. "revisit a URL from browsing history") not confirmed identical | Possible mismatch | Medium |
| `extract` | `locator.text_content()` or whole-page `visible_text` join | `get_page_content` (Markdown) / `get_dom` / `search_dom` | Low-Medium — format differs (Markdown vs. BrowserAgent's own text list), needs a translation step | Medium |
| `download` | `page.expect_download()` context manager around a click, returns path | Files/export tool category, "file downloads" | Event-shape mismatch: Playwright's is a blocking wait tied to the triggering click; BrowserOS's is a separate tool call — needs the adapter to correlate a download event with the action that triggered it, not confirmed possible from docs | High |
| `wait` | `page.get_by_text(...).wait_for()`, `page.wait_for_url()`, or a capped `page.wait_for_timeout()` | Not a single documented primitive; would likely be implemented as adapter-side polling using `take_snapshot`/`get_page_content` | Real — this weakens BrowserAgent's current three wait modes to one (polled) unless BrowserOS exposes an equivalent | Medium |
| `finish` | No browser call (internal to `AgentLoop`) | N/A | None | N/A |

**Conclusion:** most single-element actions map cleanly in *concept*; `select`, `download`, and
`wait` each carry a real, non-trivial adapter-design problem that documentation alone does not
resolve. None of these are fatal, but "can BrowserOS support all of these" is **not a clean yes**
from documentation alone — it would need the Section 20 spike to confirm `select`/`download`
behavior against the real tool schema before committing to a full backend.

---

## 10. Verification Compatibility

BrowserAgent's verifier (`agent/verifier.py`, invoked from `agent/loop.py`) works purely by
diffing two `PageObservation` snapshots — URL, text, element state, `state_hash` — never by calling
into the browser backend beyond `observe()`. **This means verification compatibility reduces
entirely to observation compatibility (Section 8).** If a `BrowserOSBackend.observe()` can produce
a faithful `PageObservation` (accepting the open ref-stability question above), the exact same
verifier code runs unmodified. This is a structural strength of BrowserAgent's existing design —
verification was never coupled to Playwright specifically, only to the shape of `PageObservation`.

Download/tab/navigation-event verification (Section 9's harder mapping cases) would need the
adapter to synthesize the same fields the current backend derives from Playwright's own APIs
(e.g. a completed download's `path`); this is adapter work, not a verifier change.

---

## 11. Event Sourcing / Crash Recovery

Traced through `agent/loop.py::_reconcile_pending_intent` (Section 3, Q8) — this is the exact
resume-time logic that would interact with any backend change.

**Interesting possibility raised in the prompt: could BrowserOS's live-page survival improve
recovery?** In principle, yes, *if* it were true: if the actual tab were still open and unmodified
after BrowserAgent crashes mid-action, resume could skip the "re-navigate to the last known URL"
step entirely and just re-observe the live tab directly, closing the exact ambiguity window the
current comment describes ("this can still leave a same-page, storage-less JS mutation
unrecoverable"). **But Section 6 established this property is not demonstrated for BrowserOS's
agent-facing product, and undocumented for the other.** So this potential benefit is currently
speculative, not evidenced.

**The specific ambiguity case in the prompt — BrowserAgent crashes after the backend executed a
click but before `ACTION_RESULT` was persisted:**

- **Current Playwright behavior:** the browser process itself may or may not still be alive at
  crash time (it's a subprocess of the same machine, but not guaranteed to survive whatever killed
  the Python process — a machine-level crash kills both; a Python-level crash/exception with a
  detached browser subprocess *could* leave it running, but `AgentLoop` does not currently rely on
  or test for that). On resume, `_reconcile_pending_intent` re-navigates and re-observes rather
  than assuming anything about what survived, and treats an unconfirmed `CONSEQUENTIAL` action as
  blocked pending human review. This is intentionally conservative and backend-agnostic reasoning
  — it never trusts backend-level survival.
- **Would a BrowserOS backend make this better, worse, or unchanged?** **Unchanged, at best, given
  current evidence** — and only *potentially* better if live-tab persistence across a client crash
  turns out to be true (unconfirmed, Section 6). It would not be worse by itself, but a
  `BrowserOSBackend` would still need to implement the exact same conservative
  reconcile-by-re-observation logic, because the alternative (trust that the backend preserved
  exact pre-crash state and skip reconciliation) is not something either backend's documentation
  currently license you to assume for a `CONSEQUENTIAL` action.

**Conclusion:** event sourcing and crash recovery are unaffected either way at the architecture
level (both `EventType` semantics and the reconciliation algorithm are pure `PageObservation`/state
logic, Section 3). The one genuine *upside* BrowserOS could offer here — skip re-navigation because
the tab is still live — is unproven, not a reason to migrate today.

---

## 12. Authentication / Session UX

The scenario under evaluation: user manually logs into Canvas/Google/school portals once; no
password ever goes through Qwen; BrowserAgent reuses the resulting session.

**BrowserAgent already has this today**, via two existing, validated mechanisms working together:

1. **Persistent Playwright profile** (`launch_persistent_context(user_data_dir=...)`) — cookies and
   storage from a prior manual login survive process restarts, same Chromium-profile mechanism
   BrowserOS would use.
2. **Manual-login handoff** (`agent/auth_detect.py`, validated in Phase 5B: `looks_like_login_page()`
   detects a password field or login-keyword title/heading, short-circuits to a `login_required`
   block, and `ui/jobs.py::login_continue()` resumes the *same live browser tab/session* once the
   page no longer looks like a login page — per `docs/PHASE5B_REPORT.md`'s own description and its
   passing deterministic test `test_manual_login_wait_and_continue`).

**Would BrowserOS materially improve this?** The one dimension where it plausibly could is
*"I already log into things in my everyday browser, and I'd like BrowserAgent to use that exact
session without a separate BrowserAgent-only profile/login step at all."* That is a real, if
modest, UX improvement — but it requires exactly the "live browser BrowserAgent doesn't own"
property whose persistence semantics are unconfirmed (Section 6). Absent that confirmation, the
practical authentication UX is roughly equivalent: either way, the user performs one manual login
into a BrowserAgent-controlled (or BrowserAgent-attachable) Chromium profile, and it persists on
disk afterward. The CDP-attach approach (Section 17) gets the *same* "use my actual daily browser
session" benefit without depending on any of BrowserOS's undocumented persistence claims, since the
user's actual, already-running Chromium instance is the thing Playwright attaches to.

---

## 13. Security

**Local server exposure.** BrowserOS's MCP endpoint is documented as binding to `127.0.0.1` with
no authentication mechanism found in the fetched docs (Section 5, Q4) — the network boundary
*is* the trust boundary, identical in kind to how BrowserAgent's own `ui/app.py` FastAPI server is
explicitly bound to `127.0.0.1` only (`cli/main.py`'s `--host` default, `README.md`'s "nothing is
exposed to your network" claim). This is not a new category of risk versus what BrowserAgent
already runs — but it *is* a second local server on the machine, with its own port, and its own
53-tool attack surface, most of which (bookmarks, history, cross-site tab control, 40+ third-party
app integrations per Section 5) is far broader than anything BrowserAgent's own `ui/app.py` exposes
today.

**Prompt injection / malicious webpage risk, specific to a persistent logged-in browser.** This is
the one place BrowserOS genuinely raises the stakes versus BrowserAgent's current per-task
isolated-profile model: if the same long-lived browser instance holds live authenticated sessions
for Canvas, Gmail, etc. *and* BrowserOS's own 53-tool MCP surface (not just BrowserAgent's narrow
10-action vocabulary) is reachable by whatever process controls it, a compromised or malicious page
encountered mid-task has a theoretically larger blast radius (cross-tab, cross-app-integration)
than BrowserAgent's current single-page, single-profile-per-task Playwright session.

**Existing BrowserAgent containment, and whether it still applies.** `agent/schemas.py::classify_risk`
and the approval gate sit at `agent/loop.py`'s action-dispatch chokepoint (`_execute_action`,
confirmed at line ~937 by direct read) — they inspect the `ModelDecision`/`ElementRef` *before*
calling `self.browser.<method>()`, never a live backend object. **This means the approval/risk gate
is backend-agnostic by construction and would apply identically to a `BrowserOSBackend`** — as
long as BrowserAgent continues to route every action through `AgentLoop`'s own dispatch (Section 6's
explicit constraint: don't let Qwen talk directly to raw BrowserOS tools). If, instead, Qwen were
ever given BrowserOS's 53 raw MCP tools directly, `classify_risk`'s narrow, tuned
keyword-on-action/element-name model would not automatically apply to that much larger action
vocabulary — this is exactly the scenario Section 14's design constraint (below) exists to prevent.

**Containment recommendation, if a BrowserOS backend is ever built:** keep the existing gate
structure exactly as designed — `BrowserOSBackend` implements the same ~10-method surface
`PlaywrightBackend` does today, translated internally into whatever BrowserOS MCP calls are needed;
Qwen never sees a BrowserOS tool name, only BrowserAgent's own `ActionType` enum. Do not remove or
weaken the approval gate; do not widen the action vocabulary to match BrowserOS's full tool surface
without separately re-deriving `classify_risk` for every new action type added.

---

## 14. Performance

No new benchmark numbers were fabricated for this study — the following are explicitly **estimates**,
labeled as such, or values already measured and reported elsewhere in this repo's own docs:

- **Browser cold-start cost (current, measured elsewhere):** not independently re-measured this
  pass; `docs/PHASE5B_REPORT.md` and `PHASE5_REPORT.md` do not report a specific launch-latency
  number, so no re-use of an existing figure is possible here — this remains **unmeasured** in this
  repo's own evidence, not merely unestimated.
- **Estimate: per-task Chromium launch overhead saved by a long-running browser (either BrowserOS
  or CDP-attach)** — order of low-single-digit seconds per task, based on general Chromium
  cold-start behavior; **this is an estimate, not a measurement**, and applies equally to both
  BrowserOS and CDP-attach, since the saving comes from "don't launch a fresh browser," not from
  which long-running browser is used.
- **MCP overhead vs. direct Playwright API calls:** an HTTP-based MCP round trip per action is
  categorically higher-latency than Playwright's own protocol connection (the same tradeoff
  Playwright's own `connect_over_cdp` docstring calls out for CDP specifically: *"significantly
  lower fidelity than the Playwright protocol connection via `browser_type.connect()`"* —
  Section 17). No specific number is available from documentation for BrowserOS's MCP layer; this
  is a directional risk, not a quantified one.
- **Idle resource usage:** running a second, always-on Chromium-based application (BrowserOS)
  alongside the user's normal browser is a real, non-zero standing memory/CPU cost whether or not
  BrowserAgent is actively using it — this is qualitative, not measured in this pass.

**No practical benchmark was run in this study** — per the task's explicit "optional minimal spike"
framing (Section 19/20 below), a live performance comparison was judged unnecessary to reach a
verdict given how decisive the persistence-semantics and project-risk findings already are; a future
implementation pass should measure this properly before relying on any number above.

---

## 15. Project Maturity / Risk

Primary source: `api.github.com/repos/browseros-ai/BrowserOS`, fetched live 2026-08-27.

- **Created:** 2025-05-18 — roughly 15 months old at the time of this study.
- **Last push:** 2026-08-27 (today) — actively maintained, not stale.
- **Stars / forks:** 13,364 / 1,413 — meaningful community traction for a project this young.
- **Open issues:** 88.
- **License:** AGPL-3.0.
- **Archived:** false.
- **Release cadence, sampled (most recent 8 releases at fetch time):** `ext-browserclaw/v0.2.17.0`,
  `v0.2.16.0`, `v0.2.15.0`, `ext-agent/v0.0.139.0`, `claw-server/v0.0.46`, `v0.0.45`, `v0.0.44`,
  `agent-server/v0.0.145` — **all published within a single week**, and **every sub-package version
  number is still pre-1.0** (`0.x.y`). This is a strong, direct signal of an actively-churning,
  not-yet-API-stable project: multiple independently-versioned sub-components (`browserclaw`,
  `ext-agent`, `claw-server`, `agent-server`), released multiple times per week, none past 1.0.
- **Documentation consistency:** three different official doc pages give three different MCP port
  numbers (Section 5, Q4) — a small but concrete signal that even the documentation itself is
  outrunning its own review process.

**Risk assessment relative to Playwright.** Playwright is a Microsoft-maintained, multi-year-old,
1.x+-versioned project with a stable public API and an explicit deprecation/versioning discipline;
BrowserAgent's existing dependency on it is low-risk by comparison. Making BrowserAgent's *core*
browser-execution layer depend on a pre-1.0, multi-week-release-cadence, AGPL-3.0 project with
internally-inconsistent documentation would be a materially higher maintenance-risk trade than
what BrowserAgent has today, and should not be taken on without a benefit that is itself proven —
which, per Section 6, it currently is not.

---

## 16. Alternatives

Compared against BrowserAgent's actual requirements, not a general survey:

1. **Current Playwright persistent context (`launch_persistent_context`)** — what BrowserAgent has
   today. Profile persistence: yes. Live-tab persistence across agent-process exit: no, by design
   (browser closes with the task). Dependency risk: low (mature, stable API). Zero new work.
2. **Playwright attached to an already-running Chromium via CDP (`connect_over_cdp`)** — confirmed
   present in the installed `playwright` package (Section 17, Section 19). Gives the *browser
   lifetime independent of agent lifetime* property the prompt actually wants, using the exact same
   `PlaywrightBackend`/`PageObservation`/verifier/event-sourcing code paths BrowserAgent has already
   validated across five phases. Zero new dependency. Recommended primary path (Section 21).
3. **Raw CDP backend** (bypassing Playwright's own abstraction, talking the Chrome DevTools Protocol
   directly) — `ARCHITECTURE.md`'s own prior research (Section 4/85-90, cited above) already covers
   this: browser-use's public migration to raw CDP is real precedent, but it trades away Playwright's
   built-in actionability checks (attached/visible/stable/enabled/receives-events with auto-retry)
   for lower latency at very high call volumes — a trade this project's current call volume does
   not obviously need to make. Not recommended now; already documented as a future escape hatch if
   per-step latency becomes the bottleneck.
4. **BrowserOS** (either product) — see full study above. The one property that would justify it
   (live-tab persistence across agent disconnect) is unconfirmed/contradicted by its own docs;
   everything else it offers is either already present in BrowserAgent (profile persistence) or
   available more cheaply via option 2.
5. **A clearly superior local persistent-browser approach discovered during this research?** — No.
   Option 2 (CDP-attach) is not a "discovery" so much as Playwright's own documented feature,
   already installed, requiring no new research risk. Nothing found during BrowserOS research
   surpasses it for BrowserAgent's specific, narrow requirement set.

---

## 17. Decision Matrix

Scored 1–10 (10 = best) across the five candidate architectures the task specifies. Scores reflect
evidence gathered in this study, not aspiration.

| Dimension | Playwright only (current) | BrowserOS only | Dual: Playwright tests + BrowserOS real usage | Playwright + CDP attach | Raw CDP |
|---|---|---|---|---|---|
| Reliability | 8 — mature, validated across 5 phases | 4 — unproven for this use case, pre-1.0 | 5 — inherits BrowserOS's unproven half | 8 — same validated code paths, new attach mode only | 5 — no actionability checks, more flakiness risk |
| Persistence (live-tab, agent-independent) | 2 — explicitly not designed for this | 3 — undocumented/contradicted for the agent-facing product | 3 — same as BrowserOS-only for the "real usage" half | 8 — genuinely gives this, via a user-launched long-running Chromium | 8 — same mechanism as CDP-attach, minus Playwright's ergonomics |
| Authenticated UX | 6 — works today via profile + manual-login handoff | 6 — same net UX, unconfirmed extra benefit | 6 | 8 — reuses the user's actual running browser session directly | 7 — same idea, more DIY |
| Compatibility with current architecture | 10 — is the current architecture | 3 — needs a new backend adapter, action-mapping gaps (Section 9) | 6 — Playwright half is free, BrowserOS half still needs the adapter | 9 — same `PlaywrightBackend` class, one new start-mode | 4 — needs a new backend, loses Playwright's Locator ergonomics |
| Verification | 9 — verifier only needs PageObservation, already proven | 5 — depends on unresolved ref-stability question (Section 8) | 6 | 9 — identical to current | 6 — same ref-resolution burden as raw CDP generally |
| Crash recovery | 8 — validated, conservative-by-design | 5 — theoretical upside unproven (Section 11) | 6 | 8 — same reconciliation logic, unaffected | 6 |
| Testing | 9 — deterministic fixtures, real CI-friendly integration tests exist today | 3 — no evidence found of a fixture-friendly test mode | 8 — keeps Playwright for exactly this | 9 — CDP-attach mode is opt-in, fixtures keep using launch mode | 5 — loses Playwright's fixture/test ergonomics |
| Performance | 6 — per-task cold launch cost (Section 14) | 6 (estimate) — long-running browser saves launch cost, MCP round-trip adds action latency | 6 | 7 (estimate) — saves launch cost, keeps Playwright's low-overhead protocol connection | 7 (estimate) — lowest per-action overhead, no launch-cost saving unless also long-running |
| Complexity | 9 — nothing to add | 4 — new dependency, new adapter, new action-mapping edge cases | 5 — two backends to maintain | 8 — one new backend mode, same class | 5 — new backend, no Playwright conveniences |
| Maintenance risk | 9 — mature dependency | 3 — pre-1.0, weekly releases, doc inconsistencies (Section 15) | 5 — inherits BrowserOS's risk for half the surface | 8 — Playwright's maintenance profile unchanged | 6 — CDP itself is stable; hand-rolled abstraction is the risk |
| Security | 7 — narrow local-only UI surface, approval gate already backend-agnostic | 5 — larger local attack surface (53 tools, 40+ integrations), same trust-boundary pattern (Section 13) | 6 | 7 — same as current, plus whatever the user's own long-running browser already exposes | 6 |
| Cross-platform support | 8 — Playwright supports Win/macOS/Linux uniformly, already in use | 7 — BrowserOS documents Win/macOS(/Linux for non-neo) | 7 | 8 — same as current | 6 — DIY per-platform quirks |

**Column averages (unweighted, for orientation only — not a substitute for reading the rows):**
Playwright-only ≈ 7.4, BrowserOS-only ≈ 4.5, Dual ≈ 5.7, Playwright+CDP-attach ≈ 8.1, Raw CDP ≈ 5.9.

---

## 18. Twenty Falsification Questions — Answered Individually

1. **Does BrowserOS actually keep the browser alive independently of BrowserAgent?** Yes, in the
   trivial sense that it's a separate application process the user starts and stops — that much is
   true by construction, same as any standalone browser.
2. **Does it actually preserve live tabs?** **Not demonstrated.** Contradicted for BrowserOS neo's
   own AI-session tabs (Section 6); undocumented for plain BrowserOS.
3. **Can BrowserAgent reconnect later?** The MCP endpoint should still be reachable if the
   application is still running (Section 5, Q34-35 note this is not explicitly documented either),
   but "reconnect to the endpoint" and "find your previous tab still there" are different claims —
   only the first is reasonably inferable; the second is the unproven one (Section 6).
4. **Can we use it without Claude?** Yes — Streamable HTTP MCP is documented as usable by any
   compliant client (Section 5, Q7-8).
5. **Can Qwen3-8B remain our only model?** Yes — BrowserOS would only ever be a controlled browser
   process behind a `BrowserOSBackend` adapter; nothing about it requires or benefits from changing
   the decision-making model (Section 5, Q9-11).
6. **Can BrowserOS run locally?** Yes (Section 5, Q10-11).
7. **Can we reuse existing authenticated sessions?** Yes, at the profile-persistence tier (same as
   Playwright already provides); the harder "same live session across agent restarts" claim is
   unproven (Section 6).
8. **Can we preserve BrowserAgent's strict action schema?** Yes — nothing about BrowserOS forces
   exposing its raw tool surface to Qwen; the adapter pattern (Section 6/8/13) fully preserves it.
9. **Can we preserve numeric target IDs?** Yes, by design — they are already BrowserAgent-owned and
   reassigned per observation (Section 3, Q5); a `BrowserOSBackend` would keep doing exactly that,
   contingent on the unresolved ref-stability question (Section 8) not making this expensive.
10. **Can we preserve deterministic verification?** Yes — verification only needs `PageObservation`
    (Section 10), backend-agnostic already.
11. **Can we preserve event sourcing?** Yes — unaffected at the architecture level (Section 11).
12. **Can we preserve crash/resume?** Yes — same reconciliation algorithm applies; the one
    *potential* improvement (skip re-navigation because the tab survived) is unproven, not required
    (Section 11).
13. **Can we preserve batch orchestration?** Yes — `batch/orchestrator.py` never imports
    `playwright`; it drives `AgentLoop` the same way regardless of backend (Section 2/3).
14. **Can we preserve research discovery?** Yes, with a caveat — `research/discovery.py` constructs
    `PlaywrightBackend` directly today (Section 3); it would need the same backend-selection change
    `agent/loop.py` gets, but nothing about its logic (`extract_candidate_links`,
    `select_relevant_links`) depends on Playwright specifically.
15. **Can we preserve our local UI?** Yes — `ui/app.py`/`ui/jobs.py` never touch a browser object
    directly; they drive `AgentLoop`/`BatchOrchestrator`/`WorkflowOrchestrator`, unaffected by
    backend choice.
16. **Would BrowserOS fix any CURRENT verified failure?** **No confirmed case found in this study.**
    The two router bugs are backend-independent (Section 4/23). The Windows pytest/Playwright
    launch-stall issue is Playwright-*contributing* but not clearly solved by BrowserOS either — a
    different long-running local server accessed repeatedly in one pytest process could exhibit its
    own analogous flakiness; no evidence either way was found.
17. **Which verified failures would it NOT fix?** Both router bugs (Findings 7 and 8) — confirmed
    100% backend-independent by direct code reading (Section 4/23).
18. **What new failure modes would BrowserOS introduce?** (a) unresolved ref-stability risk for
    click/type actions (Section 8); (b) `select`/`download`/`wait` action-mapping gaps requiring new
    adapter design, not just a drop-in translation (Section 9); (c) a materially larger local attack
    surface if the raw 53-tool MCP surface were ever exposed to Qwen instead of gated behind the
    adapter (Section 13); (d) dependency on a pre-1.0, fast-moving, occasionally
    internally-inconsistent-in-its-own-docs project (Section 15).
19. **Is BrowserOS mature enough to be a core dependency?** **No**, per the maturity evidence in
    Section 15 (pre-1.0 sub-packages, multi-times-per-week releases, doc inconsistencies) — it is
    reasonable as an optional, clearly-isolated integration to revisit later, not as BrowserAgent's
    core execution layer today.
20. **Is a dual-backend architecture justified?** **Not by current evidence.** The specific benefit
    a BrowserOS backend would add over CDP-attach (Section 17's dimension-by-dimension comparison)
    is not established; adding a second backend means maintaining two action-mapping surfaces
    (Section 9) for a benefit that is currently unproven. Revisit if/when BrowserOS's persistence
    claims for its agent-facing product are independently confirmed to hold across a real
    disconnect/reconnect cycle.

---

## 19. Recommended Architecture

**Primary recommendation:** extend `PlaywrightBackend` with a second start-mode —
`connect_over_cdp(endpoint_url)` — selectable by config, alongside the existing
`launch_persistent_context(...)` mode. This is the smallest possible change that captures the
actual desired property (browser lifetime independent of BrowserAgent process lifetime) with zero
new dependencies.

**Primary-source confirmation, verified against the installed package in this repo's own `.venv`
during this study** (not from memory): `playwright.async_api.BrowserType.connect_over_cdp()`
docstring, read directly from `.../site-packages/playwright/async_api/_generated.py:17085-17128`:

> *"This method attaches Playwright to an existing browser instance using the Chrome DevTools
> Protocol. The default browser context is accessible via `browser.contexts()`."*
>
> ```py
> browser = await playwright.chromium.connect_over_cdp("http://localhost:9222")
> default_context = browser.contexts[0]
> page = default_context.pages[0]
> ```

Every line of `PlaywrightBackend` after `self.page = ...` (click/type/select/scroll/back/extract/
download/wait/observe) operates on `self.page`, a plain Playwright `Page` object — **identical**
whether that `Page` came from `launch_persistent_context()` or `connect_over_cdp()`. This means the
entire rest of `PlaywrightBackend`, all of `PageObservation`, the verifier, event sourcing, crash
recovery, and every existing test fixture requires **zero changes**. The only new code is: (a) a
config flag choosing attach-vs-launch, (a few lines in `start()`), and (b) a decision about
`close()` behavior in attach mode (disconnect, don't kill the user's browser).

**What this does *not* require:** BrowserOS, any new dependency, any new network protocol layer, any
AGPL exposure, any new adapter for `select`/`download`/`wait` semantic gaps (Section 9), any
resolution of the ref-stability question (Section 8) — because Playwright's own `Locator`/`nth`
re-resolution mechanism (already in use today) is unaffected by attach mode.

**What this does require, that the user must set up themselves (out of scope for this study, and
explicitly not implemented per the task's own "do not implement" instruction):** launching a
Chromium-based browser with a remote-debugging port enabled and keeping it running independently of
BrowserAgent — either plain Chrome/Chromium with `--remote-debugging-port=9222`, or, if the user
specifically wants BrowserOS's UI chrome/ad-blocking/etc. as their daily driver, BrowserOS's own
Chromium binary launched the same way (nothing in this recommendation *excludes* using BrowserOS's
browser shell as the thing Playwright attaches to — it only rejects depending on BrowserOS's *MCP
control layer* as BrowserAgent's execution mechanism).

**BrowserOS as a backend: not recommended now, but not permanently closed.** If, in the future,
BrowserOS's documentation is updated (or independently verified via a live spike, Section 20) to
confirm live-tab survival across an MCP client disconnect/reconnect for its non-neo product, and
element-ref stability across calls is confirmed, `ADD_BROWSEROS_BACKEND` becomes a reasonable
revisit — the codebase's coupling is already thin enough (Section 3) that this would not be an
expensive change *then*. It is not justified *now* because the one property that would make it
worth the dependency risk (Section 15) is unproven.

---

## 20. Minimal Implementation / Spike Plan

Not implemented this pass, per instruction. If pursued later:

**Phase BO-1: BrowserBackend abstraction (small, low-risk, worth doing regardless of BrowserOS)**
Formalize the already-de-facto interface (Section 3) as an explicit `Protocol`/ABC in
`browser/backend.py`: `start()`, `close()`, `observe() -> PageObservation`, `open_url(url)`,
`click(obs, target_id)`, `type(obs, target_id, text)`, `select(obs, target_id, value)`,
`scroll(direction, amount_px)`, `back()`, `extract(obs, target_id) -> str`,
`download(obs, target_id) -> dict`, `wait(params)`. All async, matching current signatures exactly.
`PlaywrightBackend` implements it unchanged (it already satisfies this shape). **Pass gate:** all
215 existing deterministic tests still pass with zero behavior change; `agent/loop.py` and
`research/discovery.py` type-check against the new `Protocol`.

**Phase BO-2: Playwright CDP-attach mode (the actual recommendation, Section 19)**
Add `connect_over_cdp` as a second start-mode on `PlaywrightBackend`, config-selected. **Pass
gate:** a manual spike — launch a plain Chromium with `--remote-debugging-port`, run one existing
fixture-based integration test (e.g. `test_phase1_browser_actions.py`'s scenario) against the
attached instance instead of a launched one, confirm identical pass/fail behavior; confirm `close()`
in attach mode disconnects without killing the user's browser (verify the browser process and its
tabs are still there after BrowserAgent exits).

**Phase BO-3: minimal BrowserOSBackend (only if BO-4 below passes)**
A thin `browser/browseros_backend.py` implementing the same `Protocol` from BO-1, translating each
method into the corresponding BrowserOS MCP tool call (Section 9's mapping table), with the
`select`/`download`/`wait` gaps resolved against the *real* tool schema (not documentation alone —
Section 9 flagged these as unresolved from docs). **Pass gate:** the exact same fixture-based
integration test suite `PlaywrightBackend` passes today, passes unmodified against
`BrowserOSBackend` (proves `PageObservation`/verifier compatibility empirically, not just
architecturally).

**Phase BO-4: persistence spike (must happen before BO-3, not after — this is the load-bearing
unresolved question from Section 6)**
The exact experiment sketched in the task: start BrowserOS (or BrowserOS neo) independently → an
external process connects via MCP → opens a page → observes it → clicks one harmless element →
disconnects → BrowserOS remains open → a *new* client process reconnects → confirm the *same tab*,
with the click's effect still visible, still exists. **Pass gate:** this must succeed for a
BrowserOS backend to be worth building at all — if the tab is gone or reset on reconnect (consistent
with what `neo/tabs-and-isolation.md` already suggests for agent-session tabs), stop here; BO-3
is not justified, and CDP-attach (BO-2) remains the answer.

**Phase BO-5: backend parity tests**
Run the *same* deterministic test suite (215 tests) against `BrowserOSBackend` where feasible (some
tests may need a BrowserOS-reachable fixture server rather than a bare HTML file, depending on how
BrowserOS's tools handle `file://` URLs — not confirmed in this study). **Pass gate:** parity within
an explicitly agreed tolerance (e.g. no new failures beyond documented, justified action-mapping
gaps from Section 9).

**Phase BO-6: authenticated real-world tests**
One real authenticated site, read-only, manually logged in ahead of time, walked through via
`BrowserOSBackend` through the existing `browser-agent ui`. **Pass gate:** completes with the same
evidence-backed-result quality Phase 5B already established for the Playwright path, with the
manual-login handoff either not needed at all (session already live) or working identically to
today if it is needed.

**Phase BO-7: decision whether BrowserOS becomes a supported second backend**
Only after BO-4 through BO-6 produce real, not documentation-inferred, evidence. Do not skip BO-4 to
get to this decision faster — it is the one question this entire study could not resolve from
primary sources alone.

---

## 21. What NOT to Change

- Do not modify `PageObservation`/`ElementRef` to accommodate BrowserOS or CDP-attach — both are
  already backend-agnostic (Section 3, Q4-5).
- Do not expose BrowserOS's raw 53-tool MCP surface to Qwen under any circumstance — route
  everything through the existing `AgentLoop` action-dispatch chokepoint, exactly as `PlaywrightBackend`
  is routed today (Section 6, Section 13).
- Do not weaken or bypass `agent/schemas.py::classify_risk` or the approval gate for either backend
  (Section 13).
- Do not remove or alter the crash-recovery reconciliation logic in
  `agent/loop.py::_reconcile_pending_intent` on the assumption that a new backend guarantees
  live-tab survival — that assumption is unproven (Section 6, Section 11).
- Do not fix Findings 7/8 (the router bugs) as part of any backend work — they are unrelated
  (Section 4, Section 23) and were explicitly out of scope for this research task.
- Do not commit this document's branch into `main`. It stays on
  `research/browseros-backend-feasibility`, unmerged, per instruction.

---

## 22. Router Bugs: Backend Independence (Explicit Confirmation)

Both bugs were re-verified by direct code reading during this study, independent of the live UI
session where they were originally found:

- **Finding 7** (`router/extract.py:91-104`, `try_deterministic_route`'s `_looks_sequential` branch):
  `objective=stripped` is assigned identically to every `WorkflowStepPlan` in the list comprehension
  — the entire raw prompt, not a per-site sub-goal. This function receives only `text: str` as input;
  it has no browser, no backend, no network call of any kind. **Switching to BrowserOS will not fix
  this.**
- **Finding 8** (`router/extract.py:20-23`, the `_ACTION_VERBS` compiled regex): the verb list is
  `change|set|toggle|select|enable|disable|update|configure|switch|turn on|turn off` — missing
  `enter`, `type`, `fill`. `_looks_sequential()` requires both a sequence marker *and* an action-verb
  match; a "find X, then enter X on Y" prompt fails the action-verb half and falls through to
  `multisite_sweep`. Same file, same zero-backend-dependency situation. **Switching to BrowserOS
  will not fix this.**

Both are pure string-processing bugs in a module that runs entirely before any `AgentLoop`,
`PlaywrightBackend`, or browser object is ever constructed. They require a `router/extract.py` code
fix (splitting `objective` per step by re-prompting/parsing the natural-language input more
carefully, and adding the missing verbs), independent of any browser-backend decision made in this
document. Per this task's explicit scope, that fix is **not** made here.

---

## 23. Final Verdict

**USE_PLAYWRIGHT_CDP_ATTACH**

Justification, restated concisely:

1. BrowserAgent's coupling to Playwright is already unusually thin (Section 3) — this was
   evidently a deliberate Phase-1 design choice, confirmed by the codebase's own comments about
   never caching a Playwright handle. This makes *any* backend change cheap in principle, which
   removes "it's too hard to change" as a reason to avoid the better option.
2. The one property that would justify taking on a new, pre-1.0, AGPL-3.0 dependency
   (BrowserOS) — live browser/tab state surviving an agent-process disconnect and later reconnect —
   is **not demonstrated** by BrowserOS's own primary-source documentation, and is **directly
   contradicted** for its flagship agent-facing product (Section 6).
3. Playwright already, today, in the exact package version installed in this repo, provides
   `connect_over_cdp()` — a documented, first-party mechanism for attaching to an already-running,
   user-managed Chromium instance (Section 17, Section 19), which delivers the actual thing wanted
   (browser lifetime independent of BrowserAgent lifetime) without any of BrowserOS's open
   questions (ref stability, `select`/`download`/`wait` action-mapping gaps, undocumented
   reconnect semantics, internally-inconsistent port documentation, pre-1.0 maintenance risk).
4. This preserves, unchanged: `PageObservation`, numeric target IDs, the verifier, event sourcing,
   crash recovery, batch orchestration, research discovery, the local UI, and every existing
   deterministic test — the exact list the task asked to confirm preservable (Section 18,
   questions 8-15), all answered yes for this path with materially less residual risk than the
   BrowserOS path answers the same questions.
5. The two router bugs that partly motivated this inquiry are conclusively backend-independent
   (Section 22) — no backend decision, made either way, changes them.

`ADD_BROWSEROS_BACKEND` is not chosen, deliberately, per the task's own instruction not to choose it
"merely because it sounds safe" — the evidence-backed reason not to add it now is that its core
promised benefit is unproven, not that adding a second backend is inherently unsafe. If Section 20's
Phase BO-4 spike is run in the future and independently confirms live-tab persistence across a real
disconnect/reconnect cycle for BrowserOS's non-neo product, this verdict should be revisited with
that new evidence — this document is not a permanent rejection, it is a rejection given the evidence
available on 2026-08-27.
