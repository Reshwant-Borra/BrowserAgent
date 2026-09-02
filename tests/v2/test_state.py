"""Compact task state: bounded in every direction, and never loses a result."""
from __future__ import annotations

from agent_v2.state import ActionRecord, TaskState, TaskStatus


def _state(**kwargs) -> TaskState:
    return TaskState(task_id="t1", goal="collect things", **kwargs)


def test_facts_are_deduplicated_case_and_space_insensitively():
    state = _state()
    state.add_fact("The price is  $10")
    state.add_fact("the price is $10")
    assert state.facts == ["The price is $10"]


def test_overflowing_facts_spill_to_disk_instead_of_vanishing(tmp_path):
    state = _state()
    state.max_facts = 3
    spill = tmp_path / "facts.log"
    for i in range(6):
        state.add_fact(f"fact number {i}", spill)
    assert len(state.facts) == 3
    assert state.spilled_facts == 3
    assert spill.read_text(encoding="utf-8").splitlines() == [
        "fact number 0", "fact number 1", "fact number 2"]
    assert "+3 earlier facts" in state.render()


def test_completing_a_pending_item_removes_it_from_pending():
    state = _state()
    state.add_pending("check the second source")
    state.mark_completed("Check the second source")
    assert state.pending == [] and state.completed == ["Check the second source"]


def test_already_completed_work_is_not_re_added_as_pending():
    state = _state()
    state.mark_completed("search for candidates")
    state.add_pending("search for candidates")
    assert state.pending == []


def test_recent_actions_window_is_bounded():
    state = _state()
    state.max_recent_actions = 3
    for i in range(8):
        state.record_action(ActionRecord(step=i, action="click", target_name=f"n{i}",
                                         detail="", url="u", ok=True))
    assert len(state.recent_actions) == 3
    assert state.recent_actions[0].step == 5


def test_identical_consecutive_failures_are_not_repeated():
    state = _state()
    state.record_failure("click Next — the page did not change")
    state.record_failure("click Next — the page did not change")
    assert len(state.failures) == 1


def test_state_block_stays_bounded_when_every_slot_is_saturated():
    state = _state()
    for i in range(60):
        state.add_fact(f"a reasonably wordy finding number {i} with some detail attached to it")
        state.mark_completed(f"finished sub-step {i}")
        state.add_pending(f"still need to do thing {i}")
        state.record_action(ActionRecord(step=i, action="click", target_name=f"button {i}",
                                         detail="", url="https://example.com/page", ok=True))
        state.record_failure(f"failure mode {i}")
    assert state.context_tokens() < 1200


def test_round_trips_through_disk(tmp_path):
    state = _state()
    state.add_fact("kept")
    state.record_action(ActionRecord(step=1, action="open_url", target_name=None,
                                     detail="https://x", url="https://x", ok=True))
    state.metrics.llm_calls = 7
    state.status = TaskStatus.WAITING_FOR_USER.value
    path = tmp_path / "state.json"
    state.save(path)

    loaded = TaskState.load(path)
    assert loaded.facts == ["kept"]
    assert loaded.recent_actions[0].action == "open_url"
    assert loaded.metrics.llm_calls == 7
    assert loaded.status == TaskStatus.WAITING_FOR_USER.value
