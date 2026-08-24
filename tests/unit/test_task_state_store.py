"""Phase 3 core guarantee: task_state is a derived view, never authoritative. These tests
use only sqlite (no browser, no model) to prove replay/rebuild works in isolation."""
from __future__ import annotations

from memory.event_store import EventStore, EventType
from memory.task_state import TaskStateStore


def make_store(tmp_path):
    es = EventStore(tmp_path / "task.db")
    store = TaskStateStore(es)
    return es, store


def test_state_rebuilds_when_row_missing(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", ["done"])
    es.append("t1", 0, EventType.TASK_CREATED, {"goal": "goal"})
    es.append("t1", 1, EventType.OBSERVATION, {"url": "http://x/1", "page_hash": "h1"})
    es.append("t1", 1, EventType.VERIFICATION_RESULT,
               {"action": "click", "target": 1, "action_fingerprint": "click:1:{}", "url": "http://x/2"},
               {"passed": True})

    # No task_state row has ever been written — load() must rebuild via replay.
    state = store.load("t1")
    assert state.current_step == 1
    assert state.recent_actions[-1]["verification"] == "pass"
    es.close()


def test_state_rebuilds_when_row_is_stale(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    es.append("t1", 0, EventType.TASK_CREATED, {"goal": "goal"})
    state = store.load("t1")
    store.save(state)  # last_event_id now matches

    # A new event is appended "behind the back" of the cached row (simulating another
    # writer, or simply the row not having been resaved after an append).
    es.append("t1", 1, EventType.OBSERVATION, {"url": "http://x/2", "page_hash": "h2"})

    reloaded = store.load("t1")
    assert reloaded.current_url == "http://x/2"
    assert reloaded.last_event_id == es.max_event_id("t1")
    es.close()


def test_state_rebuilds_when_row_deleted_directly(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    es.append("t1", 0, EventType.TASK_CREATED, {"goal": "goal"})
    es.append("t1", 1, EventType.OBSERVATION, {"url": "http://x/1", "page_hash": "h1"})
    store.save(store.load("t1"))

    # Simulate row corruption/loss.
    es.conn.execute("DELETE FROM task_state WHERE task_id = ?", ("t1",))
    es.conn.commit()

    rebuilt = store.load("t1")
    assert rebuilt.current_url == "http://x/1"
    es.close()
