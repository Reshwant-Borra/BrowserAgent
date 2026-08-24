from __future__ import annotations

from agent.loop_detector import (
    action_fingerprint,
    detect_modal_obstruction,
    detect_navigation_loop,
    detect_noop,
    detect_repeated_action,
)


def test_noop_detected_only_when_change_expected():
    assert detect_noop("h1", "h1", expected_change=True) is True
    assert detect_noop("h1", "h1", expected_change=False) is False
    assert detect_noop("h1", "h2", expected_change=True) is False


def test_repeated_action_limit():
    fp = action_fingerprint("click", 5, {})
    recent = [{"action_fingerprint": fp}] * 2
    assert detect_repeated_action(recent, fp, limit=3) is False
    recent.append({"action_fingerprint": fp})
    assert detect_repeated_action(recent, fp, limit=3) is True


def test_repeated_action_breaks_on_different_action():
    fp = action_fingerprint("click", 5, {})
    recent = [{"action_fingerprint": fp}, {"action_fingerprint": "other"}, {"action_fingerprint": fp}]
    assert detect_repeated_action(recent, fp, limit=2) is False  # only 1 trailing match


def test_navigation_loop_requires_full_cycles():
    recent = [{"url": "A"}, {"url": "B"}, {"url": "A"}, {"url": "B"}]
    assert detect_navigation_loop(recent, limit=2) is True
    assert detect_navigation_loop(recent[:3], limit=2) is False  # only 3 entries, needs 4


def test_navigation_loop_ignores_three_distinct_urls():
    recent = [{"url": "A"}, {"url": "B"}, {"url": "C"}, {"url": "A"}]
    assert detect_navigation_loop(recent, limit=2) is False


def test_modal_obstruction():
    assert detect_modal_obstruction(modal_present=True, last_action_targeted_modal=False) is True
    assert detect_modal_obstruction(modal_present=True, last_action_targeted_modal=True) is False
    assert detect_modal_obstruction(modal_present=False, last_action_targeted_modal=False) is False
