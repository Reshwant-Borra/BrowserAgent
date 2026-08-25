from __future__ import annotations

from pathlib import Path

from agent.config import ContextConfig
from agent.context_builder import build_tiered_context
from agent.token_budget import count_tokens
from browser.page_model import ElementRef, PageObservation, SelectorHint
from memory.event_store import EventStore, EventType
from memory.models import TaskRecord, TaskState
from memory.task_memory import TaskMemoryStore


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
