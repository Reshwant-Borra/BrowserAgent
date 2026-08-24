"""Deterministic failure/loop detectors (ARCHITECTURE.md §17). None of these ask the model
whether something is wrong — they operate purely on the recent-actions window and the
current/previous page state, which is exactly why they still work after a crash+resume
(they're recomputed from persisted state, not from a live conversation)."""
from __future__ import annotations


def action_fingerprint(action: str, target: int | None, params: dict) -> str:
    """Identifies "the same action" across attempts for repeated-action/idempotency logic.
    Deliberately excludes confidence/reason (non-semantic fields)."""
    stable_params = {k: v for k, v in sorted(params.items()) if k not in ("text",)}
    return f"{action}:{target}:{stable_params}"


def detect_noop(pre_hash: str, post_hash: str, expected_change: bool) -> bool:
    """A no-op is: we expected the page to change (expected_result was non-empty) and it
    didn't. If the model asserted nothing (read-only action), an unchanged hash is normal."""
    return expected_change and pre_hash == post_hash


def detect_repeated_action(recent_actions: list[dict], fingerprint: str, limit: int) -> bool:
    """Same action chosen `limit` times in a row (regardless of whether each attempt was
    itself a no-op) — the model is stuck, not just unlucky once."""
    count = 0
    for r in reversed(recent_actions):
        if r.get("action_fingerprint") == fingerprint:
            count += 1
        else:
            break
    return count >= limit


def detect_navigation_loop(recent_actions: list[dict], limit: int) -> bool:
    """A -> B -> A -> B ... pattern: the last `2*limit` URLs alternate between exactly two
    distinct values. Requires at least `limit` full A->B->A->B cycles to fire, so a single
    legitimate back-and-forth doesn't trip it."""
    urls = [r.get("url") for r in recent_actions if r.get("url")]
    window = urls[-(2 * limit):]
    if len(window) < 2 * limit or limit < 1:
        return False
    distinct = set(window)
    if len(distinct) != 2:
        return False
    a, b = window[0], window[1]
    expected = [a if i % 2 == 0 else b for i in range(len(window))]
    return window == expected


def detect_modal_obstruction(modal_present: bool, last_action_targeted_modal: bool) -> bool:
    return modal_present and not last_action_targeted_modal
