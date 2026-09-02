"""Phase 4 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18:
"Delegation to Existing Batch / Workflow / Research Capabilities") integration tests.

Batch/workflow children are driven by an in-process FakeRunner (same style already used by
tests/unit/test_batch_orchestrator.py / test_workflow_orchestrator.py) rather than a real
AgentLoop — this exercises the real BatchOrchestrator/WorkflowOrchestrator/BatchStore/
WorkflowStore persistence and this controller's own resolution/ingestion/crash-recovery code
without needing a live model or a real browser for every batch/workflow item. The
discover_sources path is the one exception: it drives research/discovery.py's real Playwright
enumeration against a real local fixture page, with only the model's own small structured
selection call scripted (mirroring test_general_controller.py's own
`ScriptedLlamaClient`/`_SequencedSchemaClient` pattern for the controller's planner calls).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from agent.config import AppConfig
from agent.controller import GeneralAgentController
from batch.store import BatchStore
from batch.models import BatchPolicy, ResultContract
from inference.llama_client import CompletionResult
from memory.event_store import EventStore, EventType
from memory.models import TaskState
from memory.task_state import TaskStateStore
from workflow.models import WorkflowPolicy


class _SequencedSchemaClient:
    """Same fake as test_general_controller.py's own — routes by json_schema title, each
    title holding an ordered queue of scripted responses (initial plan, replans, completion
    evaluations, and research/discovery.py's own LinkSelection call all share one client)."""

    def __init__(self, responses_by_title: dict[str, list]):
        self._queues = {k: list(v) for k, v in responses_by_title.items()}
        self.endpoint = "fake://controller"
        self.calls: list[str] = []

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        queue = self._queues.get(title)
        if not queue:
            raise AssertionError(f"schema {title!r} ran out of scripted responses (prompt: {prompt[:150]!r})")
        raw = queue.pop(0)
        return CompletionResult(text=json.dumps(raw), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _config(tmp_config: AppConfig, **agent_overrides) -> AppConfig:
    for key, value in agent_overrides.items():
        setattr(tmp_config.agent, key, value)
    return tmp_config


_SATISFIED = {"satisfied": True, "missing_requirements": [], "unsupported_claims": [], "next_recommendation": "finish"}


class FakeBatchChildRunner:
    """Mirrors tests/unit/test_batch_orchestrator.py's own FakeRunner: writes a real,
    persisted child task.db per item (so BatchOrchestrator's own extraction/normalization code
    runs unmodified) without ever touching Playwright or a live model."""

    def __init__(self, outcomes: dict[str, dict[str, Any]]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def run_child(self, config, batch_id, work_item, child_goal, success_criteria,
                         profile_dir, max_steps, resume_task_id=None, runtime_policy=None,
                         approval_callback=None, seed_facts=None) -> str:
        self.calls.append(work_item["target"])
        outcome = self.outcomes[work_item["target"]]
        task_id = resume_task_id or uuid.uuid4().hex[:12]
        _write_batch_child_task(config, task_id, work_item["target"], outcome)
        return task_id


def _write_batch_child_task(config, task_id: str, target: str, outcome: dict[str, Any]) -> None:
    db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
    store = EventStore(db_path)
    try:
        store.create_task(task_id, f"child {target}", [])
        store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"child {target}", "success_criteria": []})
        store.append(task_id, 1, EventType.TASK_COMPLETED, {
            "result": outcome.get("summary", "done"), "final_url": target, "final_title": "Fixture",
            "final_text_excerpt": outcome.get("final_text_excerpt", "evidence"),
            "structured_result": outcome.get("structured_result"),
        })
        state = TaskState(task_id=task_id, current_step=1, status="completed", last_event_id=store.max_event_id(task_id))
        TaskStateStore(store).save(state)
    finally:
        store.close()


class FakeWorkflowChildRunner:
    def __init__(self, outcomes_by_target: dict[str, dict[str, Any]]):
        self.outcomes = outcomes_by_target
        self.calls: list[dict[str, Any]] = []

    async def run_child(self, config, batch_id, work_item, child_goal, success_criteria,
                         profile_dir, max_steps, resume_task_id=None, runtime_policy=None,
                         approval_callback=None, seed_facts=None) -> str:
        self.calls.append({"target": work_item["target"], "seed_facts": seed_facts})
        outcome = self.outcomes[work_item["target"]]
        task_id = uuid.uuid4().hex[:12]
        db_path = Path(config.storage.tasks_dir) / task_id / "task.db"
        store = EventStore(db_path)
        try:
            store.create_task(task_id, f"step {work_item['target']}", [])
            store.append(task_id, 0, EventType.TASK_CREATED, {"goal": f"step {work_item['target']}", "success_criteria": []})
            store.append(task_id, 1, EventType.TASK_COMPLETED, {
                "result": outcome["summary"], "final_url": work_item["target"], "final_title": "Fixture",
                "final_text_excerpt": "evidence", "verified": outcome["verified"], "outputs": outcome.get("outputs", []),
            })
            state = TaskState(task_id=task_id, current_step=1, status="completed", last_event_id=store.max_event_id(task_id))
            TaskStateStore(store).save(state)
        finally:
            store.close()
        return task_id


async def test_deterministic_batch_delegation_from_explicit_urls(tmp_config):
    """Section 8.1's "deterministic substrate selection before using the LLM": a goal listing
    >= batch_delegation_min_targets literal URLs skips the initial planning call entirely and
    goes straight to a real delegate_batch run."""
    config = _config(tmp_config, batch_delegation_min_targets=3)
    urls = [f"http://a{i}.test/page" for i in range(3)]
    goal = f"Check each of these pages and report anything relevant: {' '.join(urls)}"
    outcomes = {
        u: {"summary": f"found item {i}", "structured_result": {
            "relevant": True, "summary": f"found item {i}",
            "findings": [{"type": "item", "title": f"Item {i}", "value": f"Item {i}", "evidence": f"Item {i} listed", "source_url": u}],
        }}
        for i, u in enumerate(urls)
    }
    runner = FakeBatchChildRunner(outcomes)
    planner_client = _SequencedSchemaClient({"CompletionEvaluation": [_SATISFIED]})
    controller = GeneralAgentController.create_new(
        config, goal, [], llama_client=planner_client, child_runner=runner,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        # The initial planning call was skipped entirely — only the (single, mandatory)
        # completion-evaluation call was ever made.
        assert planner_client.calls == ["CompletionEvaluation"]
        assert sorted(runner.calls) == sorted(urls)

        events = controller.event_store.all_events(controller.control_task_id)
        started = [e for e in events if e.type == EventType.DELEGATE_STARTED]
        results = [e for e in events if e.type == EventType.DELEGATE_RESULT]
        assert len(started) == 1 and started[0].payload["substrate"] == "batch"
        assert len(results) == 1 and results[0].payload["completed"] == 3

        workspace = controller.workspace_store.load(controller.control_task_id)
        entities = [e for e in workspace.entities if e.entity_type == "item"]
        assert len(entities) == 3
        assert all((e.name or "").startswith("Item") for e in entities)
        assert all(e.attributes.get("title", "").startswith("Item") for e in entities)
        assert len(workspace.evidence) == 3
    finally:
        controller.close()


async def test_batch_delegate_no_resolvable_targets_blocks(tmp_config):
    config = _config(tmp_config, batch_delegation_min_targets=99, max_replans=0)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "delegate_batch", "reason_code": "independent_targets",
             "active_subgoal": "check the independent targets", "plan": None, "resource_refs": [],
             "clarification_question": None, "completion_claim": None},
        ],
    })
    controller = GeneralAgentController.create_new(
        config, "goal with no urls at all", [], llama_client=planner_client,
        child_runner=FakeBatchChildRunner({}),
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert "resource_missing" in (state.blocked_reason or "")
    finally:
        controller.close()


async def test_batch_delegate_crash_before_execution_is_resumable(tmp_config):
    """Crashes right after the batch is durably created (DELEGATE_STARTED appended, BatchStore
    committed to disk) but before any work item ever ran — the exact boundary this phase's
    `_reconcile_dangling_delegate` substrate branch exists for. A fresh controller instance
    must resume the SAME batch (same batch_id/dir), never start a duplicate, and the resumed
    run must ingest the delegate's real results."""
    config = _config(tmp_config)
    urls = [f"http://b{i}.test/page" for i in range(3)]
    outcomes = {
        u: {"summary": f"ok {i}", "structured_result": {
            "relevant": True, "summary": f"ok {i}",
            "findings": [{"type": "item", "field": "name", "value": f"Thing {i}", "evidence": "found", "source_url": u}],
        }}
        for i, u in enumerate(urls)
    }
    goal = f"Check each of these pages: {' '.join(urls)}"
    controller = GeneralAgentController.create_new(config, goal, [], llama_client=_SequencedSchemaClient({}))
    control_task_id = controller.control_task_id
    task = controller.state_store.get_task_record(control_task_id)

    active = "check each target"
    controller._mark_subgoal_active(active)
    targets = controller._resolve_batch_targets(task)
    assert sorted(targets) == sorted(urls)
    batch_id = f"{control_task_id}_batch_test"
    batch_dir = controller.tasks_dir / control_task_id / "delegates" / batch_id
    controller._append(EventType.DELEGATE_STARTED, {
        "substrate": "batch", "subgoal": active, "child_task_id": batch_id,
        "target_count": len(targets), "delegate_dir": str(batch_dir),
    })
    store = BatchStore.for_batch_dir(batch_dir)
    store.create_batch(active, targets, ResultContract(), BatchPolicy(), batch_id=batch_id)
    store.close()
    events_before = controller.event_store.all_events(control_task_id)
    assert any(e.type == EventType.DELEGATE_STARTED for e in events_before)
    assert not any(e.type == EventType.DELEGATE_RESULT for e in events_before)
    controller.close()  # simulated crash — orchestrator.run() never happened

    resumed = GeneralAgentController.resume(
        config, control_task_id, llama_client=_SequencedSchemaClient({"CompletionEvaluation": [_SATISFIED]}),
        child_runner=FakeBatchChildRunner(outcomes),
    )
    try:
        final_state = await resumed.run()
        assert final_state.status == "completed"
        events_after = resumed.event_store.all_events(control_task_id)
        started = [e for e in events_after if e.type == EventType.DELEGATE_STARTED]
        results = [e for e in events_after if e.type == EventType.DELEGATE_RESULT]
        # No duplicate batch was ever started — the only DELEGATE_STARTED is the one from
        # before the simulated crash, resolving to exactly one DELEGATE_RESULT.
        assert len(started) == 1
        assert len(results) == 1
        assert results[0].payload["child_task_id"] == batch_id
        assert results[0].payload["completed"] == 3
        workspace = resumed.workspace_store.load(control_task_id)
        assert len([e for e in workspace.entities if e.entity_type == "item"]) == 3
    finally:
        resumed.close()


async def test_batch_delegate_crash_mid_batch_resumes_without_redoing_completed_items(tmp_config):
    """A stronger crash boundary than the pre-execution one above: one of three items is
    already fully completed (durably recorded in BatchStore) before the simulated crash, and
    one was claimed ("running") but never finished — the real in-flight shape a kill -9 would
    leave. Resuming must never redo the completed item and must still drive the batch to a
    clean finish, exercising BatchOrchestrator's own pre-existing reconcile_running_items()
    through this controller's new resume path rather than a fresh, from-scratch run."""
    config = _config(tmp_config)
    urls = [f"http://c{i}.test/page" for i in range(3)]
    outcomes = {
        u: {"summary": f"ok {i}", "structured_result": {
            "relevant": True, "summary": f"ok {i}",
            "findings": [{"type": "item", "title": f"Thing {i}", "value": f"Thing {i}", "evidence": "found", "source_url": u}],
        }}
        for i, u in enumerate(urls)
    }
    goal = f"Check each of these pages: {' '.join(urls)}"
    controller = GeneralAgentController.create_new(config, goal, [], llama_client=_SequencedSchemaClient({}))
    control_task_id = controller.control_task_id
    task = controller.state_store.get_task_record(control_task_id)

    active = "check each target"
    controller._mark_subgoal_active(active)
    targets = controller._resolve_batch_targets(task)
    batch_id = f"{control_task_id}_batch_test2"
    batch_dir = controller.tasks_dir / control_task_id / "delegates" / batch_id
    controller._append(EventType.DELEGATE_STARTED, {
        "substrate": "batch", "subgoal": active, "child_task_id": batch_id,
        "target_count": len(targets), "delegate_dir": str(batch_dir),
    })
    store = BatchStore.for_batch_dir(batch_dir)
    store.create_batch(active, targets, ResultContract(), BatchPolicy(), batch_id=batch_id)
    # Item 0 fully completes before the crash (real BatchOrchestrator machinery, one item).
    pre_runner = FakeBatchChildRunner(outcomes)
    pre_orchestrator_config = config
    item0 = dict(store.claim_next_item(batch_id, "local", 900))
    assert item0["target"] == targets[0]
    child_task_id = await pre_runner.run_child(
        pre_orchestrator_config, batch_id, item0, "child goal", [], None, 20,
    )
    from batch.orchestrator import _extract_structured_result
    from memory.event_store import EventStore as _ES
    child_events_store = _ES(Path(config.storage.tasks_dir) / child_task_id / "task.db")
    events = child_events_store.all_events(child_task_id)
    child_events_store.close()
    parsed = _extract_structured_result(events, item0["target"], child_task_id, require_json=False)
    result_id = store.upsert_result(
        batch_id, item0["id"], item0["target"], "completed", parsed["summary"],
        parsed["structured_data"], item0["target"], parsed.get("final_url"), parsed["evidence"],
        child_task_id, parsed["source_event_ids"],
    )
    store.complete_item(item0["id"], result_id)
    # Item 1 is claimed ("running") but never finishes — the true kill -9 shape.
    item1 = dict(store.claim_next_item(batch_id, "local", 900))
    assert item1["target"] == targets[1]
    store.close()
    controller.close()  # simulated crash

    resumed = GeneralAgentController.resume(
        config, control_task_id, llama_client=_SequencedSchemaClient({"CompletionEvaluation": [_SATISFIED]}),
        child_runner=FakeBatchChildRunner(outcomes),
    )
    try:
        final_state = await resumed.run()
        assert final_state.status == "completed"
        events_after = resumed.event_store.all_events(control_task_id)
        started = [e for e in events_after if e.type == EventType.DELEGATE_STARTED]
        results = [e for e in events_after if e.type == EventType.DELEGATE_RESULT]
        assert len(started) == 1
        assert len(results) == 1
        assert results[0].payload["completed"] == 3
        workspace = resumed.workspace_store.load(control_task_id)
        assert len([e for e in workspace.entities if e.entity_type == "item"]) == 3
    finally:
        resumed.close()


async def test_delegate_workflow_preserves_ordered_fact_passing(tmp_config):
    urls = ["http://site-a.test/lookup", "http://site-b.test/config"]
    goal = f"Find the code on {urls[0]} then enter that exact code on {urls[1]}."
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "delegate_workflow", "reason_code": "ordered_dependency",
             "active_subgoal": "find the code, then enter it on the config page",
             "plan": ["extract the code from the lookup page", "enter that code on the config page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [_SATISFIED],
    })
    runner = FakeWorkflowChildRunner({
        urls[0]: {"summary": "found code AX-42", "verified": True, "outputs": [{"key": "code", "value": "AX-42"}]},
        urls[1]: {"summary": "entered AX-42 and verified", "verified": True, "outputs": []},
    })
    controller = GeneralAgentController.create_new(
        config=_config(tmp_config), goal=goal, success_criteria=[],
        llama_client=planner_client, child_runner=runner,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        assert [c["target"] for c in runner.calls] == urls
        assert runner.calls[1]["seed_facts"] == {"code": "AX-42"}

        events = controller.event_store.all_events(controller.control_task_id)
        started = [e for e in events if e.type == EventType.DELEGATE_STARTED]
        results = [e for e in events if e.type == EventType.DELEGATE_RESULT]
        assert len(started) == 1 and started[0].payload["substrate"] == "workflow"
        assert len(results) == 1 and results[0].payload["status"] == "completed"

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert workspace.facts.get("workflow_fact::code") == "AX-42"
    finally:
        controller.close()


async def test_delegate_workflow_blocked_step_triggers_replan(tmp_config):
    urls = ["http://site-c.test/lookup", "http://site-d.test/config"]
    goal = f"Find the code on {urls[0]} then enter that exact code on {urls[1]}."
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "delegate_workflow", "reason_code": "ordered_dependency",
             "active_subgoal": "find the code, then enter it on the config page",
             "plan": ["extract the code from the lookup page", "enter that code on the config page"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    runner = FakeWorkflowChildRunner({
        urls[0]: {"summary": "could not find a code", "verified": False, "outputs": []},
    })
    controller = GeneralAgentController.create_new(
        config=_config(tmp_config, max_replans=0), goal=goal, success_criteria=[],
        llama_client=planner_client, child_runner=runner,
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert "workflow delegate blocked" in (state.blocked_reason or "")
    finally:
        controller.close()


async def test_discover_sources_then_delegate_batch_ingests_real_candidates(tmp_config, fixture_site_url):
    """Drives the real research/discovery.py enumeration (real Playwright, real fixture page
    tests/fixtures/simple_site/search_results.html) with only the model's own small structured
    calls scripted — proving discover_sources adds real, non-hallucinated candidate URLs to
    the workspace, and a later delegate_batch (chosen by the planner once those candidates
    exist) consumes exactly them. agent/controller.py imports the module as `research_discovery`
    (`from research import discovery as research_discovery`), which is the same module object
    `import research.discovery` gives here, so patching `discovery_mod.discover_sources` below
    is visible through the controller's own reference too."""
    config = _config(tmp_config, research_discovery_max_sources=5)
    search_url = f"{fixture_site_url}/search_results.html"
    goal = "Research the Pomodoro technique from a few good sources and summarize it."
    # The real, static candidate hrefs on search_results.html (nav chrome already filtered by
    # research/discovery.py's own _NAV_TEXT_BLOCKLIST) — known ahead of time only because the
    # fixture page is static, never invented by this test's own logic.
    discovered_urls = [
        "https://example.edu/pomodoro-technique-guide",
        "https://example.org/study-methods-compared",
        "https://example.net/time-management-basics",
        "https://shop.example.com/kitchen-timers",
        "https://example.io/research-focus-productivity",
    ]
    outcomes = {
        u: {"summary": "relevant", "structured_result": {
            "relevant": True, "summary": "relevant",
            "findings": [{"field": "summary", "value": "relevant content", "evidence": "relevant content", "source_url": u}],
        }}
        for u in discovered_urls
    }

    class _DiscoveryAwareClient(_SequencedSchemaClient):
        """Extends the ordinary schema-routed fake with one extra rule: research/discovery.py's
        own LinkSelection call always selects every real candidate id actually offered in the
        prompt (parsed from the rendered "[id] ..." candidate block) — never a hard-coded id,
        since those ids only exist once the real page has actually been scanned."""

        async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
            title = (json_schema or {}).get("title", "")
            if title == "LinkSelection":
                self.calls.append(title)
                ids = [int(m) for m in re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)]
                return CompletionResult(text=json.dumps({"selected_ids": ids}), total_latency_ms=1.0)
            return await super().complete(prompt, grammar, max_tokens, json_schema)

    planner_client = _DiscoveryAwareClient({
        "ControllerDecision": [
            {"decision": "discover_sources", "reason_code": "resource_missing",
             "active_subgoal": "find sources about the Pomodoro technique", "plan": None,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
            {"decision": "delegate_batch", "reason_code": "independent_targets",
             "active_subgoal": "read each discovered source", "plan": None, "resource_refs": [],
             "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [_SATISFIED],
    })

    import research.discovery as discovery_mod
    real_discover_sources = discovery_mod.discover_sources

    async def _discover_against_fixture(cfg, client, objective, profile_dir, max_sources=5, search_engine_url=None):
        return await real_discover_sources(cfg, client, objective, profile_dir, max_sources=max_sources,
                                            search_engine_url=search_url)

    discovery_mod.discover_sources = _discover_against_fixture
    try:
        controller = GeneralAgentController.create_new(
            config, goal, [], llama_client=planner_client,
            child_runner=FakeBatchChildRunner(outcomes),
        )
        try:
            state = await controller.run()
            assert state.status == "completed"
            assert "LinkSelection" in planner_client.calls

            events = controller.event_store.all_events(controller.control_task_id)
            discovery_results = [
                e for e in events if e.type == EventType.DELEGATE_RESULT
                and e.payload.get("substrate") == "research_discovery"
            ]
            assert len(discovery_results) == 1
            assert discovery_results[0].payload["source_count"] == len(discovered_urls)

            batch_started = [
                e for e in events if e.type == EventType.DELEGATE_STARTED
                and e.payload.get("substrate") == "batch"
            ]
            assert len(batch_started) == 1
            assert batch_started[0].payload["target_count"] == len(discovered_urls)

            workspace = controller.workspace_store.load(controller.control_task_id)
            resolved_sources = [e for e in workspace.entities if e.entity_type == "discovered_source"]
            assert all(e.status == "resolved" for e in resolved_sources)
            assert len(resolved_sources) == len(discovered_urls)
        finally:
            controller.close()
    finally:
        discovery_mod.discover_sources = real_discover_sources


# ---- Acceptance-test corrective pass (docs/BROWSERAGENT_MASTER_STATUS.md's FINAL ACCEPTANCE
# section, RC-2): discover_sources must check known/open resources before ever reaching for a
# live web search in cdp_attach mode -----------------------------------------------------

async def test_discover_sources_cdp_attach_resolves_open_tabs_without_web_search(tmp_config, monkeypatch):
    """Three independent live acceptance tasks ("my open course pages", "this page", "the
    widget pages I have open") all skipped straight to a live open-web search — in one case
    navigating to and requesting approval to click into a real, unrelated third party's
    website as if it were the user's own page. router/policy.py's own route() already
    resolves exactly this class of reference deterministically against the user's actual open
    tabs (never inventing a target); this proves the controller now calls it, and never
    reaches research_discovery.discover_sources, when real tabs resolve the reference."""
    config = _config(tmp_config)
    config.browser.mode = "cdp_attach"
    resolved_urls = ["http://127.0.0.1:1/candidate_alpha.html", "http://127.0.0.1:1/candidate_beta.html"]

    async def fake_route(text, client, cfg):
        from router.schema import RouterDecision, TaskType
        return RouterDecision(task_type=TaskType.MULTISITE_SWEEP, objective=text, targets=resolved_urls)

    monkeypatch.setattr("agent.controller.route", fake_route)

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("discover_sources must not run a live web search when open tabs already resolved the reference")

    import research.discovery as discovery_mod
    monkeypatch.setattr(discovery_mod, "discover_sources", _must_not_be_called)

    goal = "Look through the widget pages I have open and tell me which is the best value."
    outcomes = {u: {"summary": "relevant", "structured_result": {
        "relevant": True, "summary": "relevant",
        "findings": [{"field": "summary", "value": "relevant content", "evidence": "relevant content", "source_url": u}],
    }} for u in resolved_urls}
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "discover_sources", "reason_code": "resource_missing",
             "active_subgoal": "find the widget pages I have open", "plan": None,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
            {"decision": "delegate_batch", "reason_code": "independent_targets",
             "active_subgoal": "inspect each open widget page", "plan": None, "resource_refs": [],
             "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [_SATISFIED],
    })
    controller = GeneralAgentController.create_new(
        config, goal, [], llama_client=planner_client,
        child_runner=FakeBatchChildRunner(outcomes),
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        events = controller.event_store.all_events(controller.control_task_id)
        assert not any(
            e.type == EventType.DELEGATE_STARTED and e.payload.get("substrate") == "research_discovery"
            for e in events
        )
        workspace = controller.workspace_store.load(controller.control_task_id)
        resolved_sources = [e for e in workspace.entities if e.entity_type == "discovered_source"]
        assert {e.name for e in resolved_sources} == set(resolved_urls)
    finally:
        controller.close()


async def test_discover_sources_cdp_attach_unresolved_reference_asks_instead_of_searching(tmp_config, monkeypatch):
    """The other half of RC-2: when nothing open actually matches the description, the
    controller must ask for clarification (matching every other dispatch path in this
    codebase via router/policy.py's NeedsInput), never silently fall through to the open
    internet and invent a target."""
    config = _config(tmp_config)
    config.browser.mode = "cdp_attach"
    question = "I don't have any matching pages open. Open them or paste the URL."

    async def fake_route(text, client, cfg):
        from router.policy import NeedsInput
        from router.plan_schema import TaskPlan, PlanIntent, ExecutionShape
        plan = TaskPlan(goal=text, intent=PlanIntent.READ, execution_shape=ExecutionShape.SINGLE)
        return NeedsInput(question=question, plan=plan)

    monkeypatch.setattr("agent.controller.route", fake_route)

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("an unresolved known-resource reference must ask, not search the open web")

    import research.discovery as discovery_mod
    monkeypatch.setattr(discovery_mod, "discover_sources", _must_not_be_called)

    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "discover_sources", "reason_code": "resource_missing",
             "active_subgoal": "find my open course pages", "plan": None,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
    })
    controller = GeneralAgentController.create_new(
        config, "Check my open course pages and tell me what's due.", [], llama_client=planner_client,
    )
    try:
        state = await controller.run()
        assert state.status == "blocked"
        assert state.blocked_reason == question
        events = controller.event_store.all_events(controller.control_task_id)
        blocked = [e for e in events if e.type == EventType.TASK_BLOCKED]
        assert blocked and blocked[-1].payload.get("kind") == "ask_user"
    finally:
        controller.close()


async def test_discover_sources_cdp_attach_genuine_web_research_still_searches(tmp_config, monkeypatch):
    """Negative control: a genuine open-web research request (no open-tab/current-page
    reference at all) must keep working exactly as before, even in cdp_attach mode — this is
    what B2-style "research this topic" acceptance tasks depend on."""
    config = _config(tmp_config)
    config.browser.mode = "cdp_attach"

    async def fake_route(text, client, cfg):
        from router.schema import RouterDecision, TaskType, SafetyPolicy
        return RouterDecision(task_type=TaskType.RESEARCH, objective=text, targets=[],
                               requires_discovery=True, preferred_policy=SafetyPolicy.READ_ONLY,
                               result_contract="research")

    monkeypatch.setattr("agent.controller.route", fake_route)

    calls: list[str] = []

    async def fake_discover_sources(cfg, client, objective, profile_dir, max_sources=5, search_engine_url=None):
        calls.append(objective)
        return ["https://example.edu/photosynthesis-overview"]

    import research.discovery as discovery_mod
    monkeypatch.setattr(discovery_mod, "discover_sources", fake_discover_sources)

    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "discover_sources", "reason_code": "resource_missing",
             "active_subgoal": "find sources about photosynthesis", "plan": None,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
            {"decision": "delegate_batch", "reason_code": "independent_targets",
             "active_subgoal": "read each discovered source", "plan": None, "resource_refs": [],
             "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [_SATISFIED],
    })
    controller = GeneralAgentController.create_new(
        config, "Research how photosynthesis works using reputable sources.", [], llama_client=planner_client,
        child_runner=FakeBatchChildRunner({
            "https://example.edu/photosynthesis-overview": {"summary": "relevant", "structured_result": {
                "relevant": True, "summary": "relevant", "findings": [
                    {"field": "summary", "value": "relevant content", "evidence": "relevant content",
                     "source_url": "https://example.edu/photosynthesis-overview"},
                ],
            }},
        }),
    )
    try:
        state = await controller.run()
        assert calls, "a genuine open-web research request must still reach research_discovery.discover_sources"
        assert state.status == "completed"
    finally:
        controller.close()


async def test_discover_sources_cdp_attach_current_page_needs_no_discovery(tmp_config, monkeypatch):
    """Bug found while rerunning the acceptance campaign after the fix above (docs/
    BROWSERAGENT_MASTER_STATUS.md's FINAL ACCEPTANCE section, RC-2 rerun): route() correctly
    classifies a "the current page"-shaped subgoal as SINGLE_SITE with empty targets — router/
    resources.py's own documented "no requirement, or only a current_page one: empty targets
    is the existing, correct signal AgentLoop already handles" shape — but the controller's
    `if not result.targets: return None` fallback treated this identically to "nothing
    resolved," incorrectly falling through to a live web search anyway for the most
    unambiguous case of all (no discovery needed whatsoever)."""
    config = _config(tmp_config)
    config.browser.mode = "cdp_attach"

    async def fake_route(text, client, cfg):
        from router.schema import RouterDecision, TaskType
        return RouterDecision(task_type=TaskType.SINGLE_SITE, objective=text, targets=[], requires_discovery=False)

    monkeypatch.setattr("agent.controller.route", fake_route)

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("a current-page reference needs no discovery at all, let alone a live web search")

    import research.discovery as discovery_mod
    monkeypatch.setattr(discovery_mod, "discover_sources", _must_not_be_called)

    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "discover_sources", "reason_code": "resource_missing",
             "active_subgoal": "identify the key information gaps on the current page", "plan": None,
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
            {"decision": "finish", "reason_code": "completion_satisfied",
             "active_subgoal": None, "plan": None, "resource_refs": [],
             "clarification_question": None, "completion_claim": "done"},
        ],
    })
    controller = GeneralAgentController.create_new(
        config, "Tell me what I still need to know from this page.", [], llama_client=planner_client,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        events = controller.event_store.all_events(controller.control_task_id)
        assert not any(
            e.type == EventType.DELEGATE_STARTED and e.payload.get("substrate") == "research_discovery"
            for e in events
        )
    finally:
        controller.close()
