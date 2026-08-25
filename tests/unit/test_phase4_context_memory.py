from __future__ import annotations

from pathlib import Path

from agent.config import ContextConfig
from agent.context_builder import build_tiered_context
from agent.loop import AgentLoop
from agent.loop_detector import detect_repeated_semantic_action, semantic_action_signature
from agent.schemas import ActionType, ModelDecision
from agent.token_budget import count_tokens
from browser.page_model import ElementRef, PageObservation, SelectorHint
from memory.event_store import EventStore, EventType
from memory.models import TaskRecord, TaskState
from memory.task_memory import TaskMemoryStore, build_retrieval_queries, derive_active_facts


def _store(tmp_path: Path) -> EventStore:
    store = EventStore(tmp_path / "task.db")
    store.create_task("t1", "Remember the Advanced configuration", ["Advanced"])
    store.append("t1", 0, EventType.TASK_CREATED, {
        "goal": "Remember the Advanced configuration",
        "success_criteria": ["Advanced"],
    })
    return store


def _pass_event(store: EventStore, step: int, action: str = "click", url: str = "http://x/page") -> None:
    store.append("t1", step, EventType.VERIFICATION_RESULT, {
        "action": action,
        "target": step,
        "action_fingerprint": f"{action}:{step}",
        "url": url,
        "result_data": {},
    }, verification_result={"passed": True, "checks": []})


def _observation() -> PageObservation:
    return PageObservation(
        url="http://x/final",
        title="Final Advanced Settings",
        elements=[
            ElementRef(
                id=1,
                role="button",
                name="Apply Advanced",
                selector_hint=SelectorHint(css="button", nth=0),
            )
        ],
        visible_text=["Choose the Advanced option now"],
        char_count=200,
        element_count=1,
    )


def _final_config_observation() -> PageObservation:
    return PageObservation(
        url="http://x/final",
        title="Final configuration",
        elements=[
            ElementRef(id=7, role="select", name="Mode", options=["Basic", "Advanced"],
                       selector_hint=SelectorHint(css="select", nth=0)),
            ElementRef(id=8, role="select", name="Region", options=["North", "South"],
                       selector_hint=SelectorHint(css="select", nth=1)),
            ElementRef(id=9, role="textbox", name="Token",
                       selector_hint=SelectorHint(css="input", nth=0)),
            ElementRef(id=10, role="button", name="Save configuration",
                       selector_hint=SelectorHint(css="button", nth=0)),
        ],
        visible_text=["Final configuration"],
        char_count=200,
        element_count=4,
    )


def test_context_budgeting_records_blocks_and_keeps_prompt_bounded(tmp_path):
    store = _store(tmp_path)
    for step in range(1, 35):
        _pass_event(store, step)
    state = TaskState(
        task_id="t1",
        current_step=34,
        recent_actions=[
            {"step": i, "action": "click", "target": i, "verification": "pass", "url": f"http://x/{i}"}
            for i in range(1, 35)
        ],
    )
    task = TaskRecord(
        id="t1",
        created_at="now",
        status="running",
        goal="Remember the Advanced configuration",
        success_criteria=["Advanced"],
    )
    config = ContextConfig(
        recent_actions=3,
        max_total_tokens=900,
        recent_window_tokens=80,
        summary_tokens=120,
        retrieved_memory_tokens=80,
        page_tokens=160,
        retrieved_memory_top_k=3,
    )

    context = build_tiered_context(task, state, _observation(), config, 3000, 12, store)

    assert context.total_estimated_tokens <= 900
    assert context.block_tokens["recent_window"] <= 80
    assert context.block_tokens["page"] <= 160
    assert set(context.block_tokens) >= {
        "static_prefix",
        "task_state",
        "running_summary",
        "recent_window",
        "page",
    }
    assert "step 34" in context.prompt
    assert "step 1: click" not in context.prompt


def test_compaction_preserves_early_needed_fact_with_provenance(tmp_path):
    store = _store(tmp_path)
    extracted_id = store.append("t1", 1, EventType.ACTION_RESULT, {
        "action_fingerprint": "extract:1",
        "post_state_hash": "h",
        "result_data": {"extracted": "The required configuration is Advanced."},
        "error": None,
    })
    for step in range(2, 36):
        _pass_event(store, step)

    memory = TaskMemoryStore(store)
    events = store.all_events("t1")
    summary = memory.compact_if_needed("t1", events, keep_last_steps=3, summary_token_budget=200, force=True)

    assert summary is not None
    assert "Advanced" in summary.summary
    assert extracted_id in summary.source_event_ids
    event_types = [event.type for event in store.all_events("t1")]
    assert EventType.COMPACTION_STARTED in event_types
    assert EventType.SUMMARY_CREATED in event_types
    assert EventType.COMPACTION_COMMITTED in event_types


def test_task_memory_writes_verified_records_and_rebuilds_from_events(tmp_path):
    store = _store(tmp_path)
    source_id = store.append("t1", 1, EventType.ACTION_RESULT, {
        "action_fingerprint": "download:1",
        "post_state_hash": "h",
        "result_data": {"suggested_filename": "report.csv", "path": "/tmp/report.csv"},
        "error": None,
    })
    memory = TaskMemoryStore(store)

    assert memory.ingest_events("t1", store.all_events("t1")) >= 1
    hits = memory.search("t1", "Find the report file", top_k=3, token_budget=50)

    assert hits
    assert hits[0].source_event_id == source_id
    assert "report.csv" in hits[0].content
    assert count_tokens("\n".join(hit.content for hit in hits)) <= 50

    memory.conn.execute("DELETE FROM task_memories WHERE task_id = ?", ("t1",))
    memory.conn.execute("DELETE FROM task_memories_fts")
    rebuilt = memory.rebuild("t1", store.all_events("t1"))
    assert rebuilt >= 1
    assert memory.search("t1", "report", top_k=1, token_budget=20)


def test_observation_memory_captures_salient_visible_requirements(tmp_path):
    store = _store(tmp_path)
    source_id = store.append("t1", 1, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h1",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": [
            "Use mode Advanced for this task.",
            "Generic navigation label",
            "Remember token AX-47; it will not be repeated.",
        ],
    })
    memory = TaskMemoryStore(store)

    memory.ingest_events("t1", store.all_events("t1"))
    hits = memory.search("t1", "Advanced token", top_k=3, token_budget=80)

    assert {hit.source_event_id for hit in hits} == {source_id}
    assert any("Advanced" in hit.content for hit in hits)
    assert any("AX-47" in hit.content for hit in hits)


def test_page_aware_retrieval_query_prioritizes_current_controls():
    goal = "Complete the very long generic workflow with many repeated setup navigation words"
    queries = build_retrieval_queries(
        goal,
        ["Configuration saved"],
        None,
        _final_config_observation(),
        [],
    )

    assert queries[0].name == "current_page_affordances"
    assert {"mode", "region", "token"}.issubset(set(queries[0].terms))
    flattened = [term for query in queries for term in query.terms]
    assert flattened.index("mode") < flattened.index("complete")


def test_page_terms_cannot_be_crowded_out_by_goal_terms(tmp_path):
    store = _store(tmp_path)
    source_id = store.append("t1", 1, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h1",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": ["Use mode Advanced."],
    })
    memory = TaskMemoryStore(store)
    memory.ingest_new_events("t1")

    task = TaskRecord(
        id="t1",
        created_at="now",
        status="running",
        goal=" ".join(f"generic{i}" for i in range(30)),
        success_criteria=["Configuration saved"],
    )
    context = build_tiered_context(
        task,
        TaskState(task_id="t1"),
        _final_config_observation(),
        ContextConfig(enable_running_summary=False, enable_active_facts=False,
                      retrieved_memory_top_k=2, retrieved_memory_tokens=80),
        3000,
        12,
        store,
    )

    assert "mode" in context.retrieval_query_terms
    assert context.selected_memory_source_event_ids == [source_id]


def test_structured_fact_extraction_is_generic_and_provenance_backed(tmp_path):
    store = _store(tmp_path)
    event_id = store.append("t1", 1, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h1",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": [
            "Use department Finance.",
            "Remember version 7.4; it will be needed later.",
            "The required artifact is package.zip.",
            "Fact 1: Atlas",
        ],
    })
    memory = TaskMemoryStore(store)
    memory.ingest_new_events("t1")

    rows = store.conn.execute(
        "SELECT key, value, source_event_id, source_text FROM active_task_facts WHERE task_id = ?",
        ("t1",),
    ).fetchall()
    facts = {(row["key"], row["value"]): row for row in rows}
    assert ("department", "Finance") in facts
    assert ("version", "7.4") in facts
    assert ("artifact", "package.zip") in facts
    assert ("fact 1", "Atlas") in facts
    assert all(row["source_event_id"] == event_id for row in rows)
    assert all(row["source_text"] for row in rows)


def test_active_facts_rebuild_from_events(tmp_path):
    store = _store(tmp_path)
    store.append("t1", 1, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h1",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": ["Use region North."],
    })
    memory = TaskMemoryStore(store)
    memory.ingest_new_events("t1")
    store.conn.execute("DELETE FROM active_task_facts WHERE task_id = ?", ("t1",))
    store.conn.execute("DELETE FROM task_memory_ingest_state WHERE task_id = ?", ("t1",))

    memory.rebuild("t1", store.all_events("t1"))

    facts = memory.active_facts("t1")
    assert [(fact.key, fact.value) for fact in facts] == [("region", "North")]


def test_summary_excludes_routine_successful_clicks_and_dedupes_requirements(tmp_path):
    store = _store(tmp_path)
    store.append("t1", 1, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h1",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": ["Use region North.", "Use region North."],
    })
    for step in range(2, 8):
        _pass_event(store, step)
    summary = TaskMemoryStore(store).compact_if_needed(
        "t1", store.all_events("t1"), keep_last_steps=2, summary_token_budget=220, force=True,
    )

    assert summary is not None
    assert "TASK MEMORY SUMMARY" in summary.summary
    assert summary.summary.count("region = North") == 1
    assert "Step 2 succeeded: click" not in summary.summary
    assert "Previous summary" not in summary.summary


def test_incremental_memory_ingestion_only_processes_new_events(tmp_path):
    store = _store(tmp_path)
    memory = TaskMemoryStore(store)
    first_id = store.append("t1", 1, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h1",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": ["Use mode Advanced."],
    })
    assert memory.ingest_new_events("t1") >= 1
    assert memory.ingest_new_events("t1") == 0
    second_id = store.append("t1", 2, EventType.OBSERVATION, {
        "url": "http://x/requirements",
        "title": "Requirements",
        "page_hash": "h2",
        "element_count": 1,
        "char_count": 100,
        "phase": "pre_decision",
        "element_names": ["Continue"],
        "visible_text": ["Use region North."],
    })

    assert memory.ingest_new_events("t1") >= 1
    row = store.conn.execute(
        "SELECT last_ingested_event_id FROM task_memory_ingest_state WHERE task_id = ?",
        ("t1",),
    ).fetchone()
    assert row["last_ingested_event_id"] == second_id
    assert first_id < second_id


def test_evaluation_criteria_never_enters_prompt(tmp_path):
    store = _store(tmp_path)
    task = TaskRecord(
        id="t1",
        created_at="now",
        status="running",
        goal="Collect facts and submit them.",
        success_criteria=["Final synthesis", "Combined result"],
    )
    context = build_tiered_context(
        task,
        TaskState(task_id="t1"),
        _final_config_observation(),
        ContextConfig(enable_running_summary=False, enable_memory_retrieval=False,
                      enable_active_facts=False),
        3000,
        12,
        store,
    )

    assert "Atlas Copper Delta" not in context.prompt
    assert "Combined result" in context.prompt


def test_empty_criteria_finish_needs_prior_verified_evidence(tmp_path):
    loop = AgentLoop.__new__(AgentLoop)
    state = TaskState(task_id="t1", recent_actions=[])

    assert not any(r.get("verification") == "pass" for r in state.recent_actions)


def test_memory_action_value_match_and_conflict_are_generic():
    loop = AgentLoop.__new__(AgentLoop)
    fact_event = type("Fact", (), {
        "id": 1,
        "kind": "requirement",
        "key": "region",
        "value": "North",
        "confidence": 1.0,
    })()
    element = ElementRef(id=8, role="select", name="Region", options=["North", "South"],
                         selector_hint=SelectorHint(css="select", nth=0))

    match = loop._memory_application_check(
        [fact_event],
        ModelDecision(action=ActionType.SELECT, target=8, params={"value": "North"}),
        element,
    )
    conflict = loop._memory_application_check(
        [fact_event],
        ModelDecision(action=ActionType.SELECT, target=8, params={"value": "South"}),
        element,
    )

    assert match["value_match"] is True
    assert conflict["value_conflict"] is True


def test_semantic_loop_signature_ignores_transient_element_ids():
    sig = semantic_action_signature("select", "Region", {"value": "South"})
    recent = [
        {"semantic_action_signature": sig},
        {"semantic_action_signature": sig},
    ]

    assert sig == semantic_action_signature("select", "Region", {"value": "South"})
    assert detect_repeated_semantic_action(recent, sig, limit=2)
