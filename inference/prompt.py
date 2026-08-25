"""Prompt section templates.

Ordered STATIC -> SEMI-STABLE -> VOLATILE per ARCHITECTURE.md's prefix-stability rule, so
that llama.cpp's prompt-cache (`cache_prompt`) can reuse the common prefix across calls.
This is purely a performance detail: `agent/context_builder.py` rebuilds every section from
`TaskState` + a fresh `PageObservation` on every call, so correctness never depends on the
cache actually hitting.
"""
from __future__ import annotations

from typing import Optional

ACTION_VOCAB = "open_url, click, type, select, scroll, back, extract, download, wait, finish"

# STATIC — identical on every call for every task; this is the part cache_prompt benefits most from.
SYSTEM_BLOCK = f"""SYSTEM
You are a browser-automation decision engine. You choose exactly one browser action per turn
from a compact structured description of the current page. You never see raw HTML or images.

AVAILABLE ACTIONS: {ACTION_VOCAB}
- open_url(url): navigate directly to a URL. No target.
- click(target): click the interactive element with that id.
- type(target, text): fill the textbox with that id with `text`.
- select(target, value): choose `value` from the dropdown with that id.
- scroll(direction): "up" or "down". No target.
- back(): go to the previous page. No target.
- extract(target|none): return visible text of one element, or the whole page if no target.
- download(target): click a link/button expected to start a file download.
- wait(for_text|url_contains|ms): wait for a condition or a short timeout.
- finish(result): the task is complete; result summarizes the outcome.

OUTPUT CONTRACT
Respond with exactly one JSON object, nothing else. Use only fields needed by that action:
{{"action":"open_url","url":"http://example.com"}}
{{"action":"click","target":17}}
{{"action":"type","target":8,"text":"alpha"}}
{{"action":"select","target":22,"value":"Advanced"}}
{{"action":"scroll","direction":"down"}}
{{"action":"back"}}
{{"action":"extract","target":17}}
{{"action":"download","target":31}}
{{"action":"wait","for_text":"Done"}}
{{"action":"finish","result":"Done."}}

Element IDs are the numbers in square brackets. If you act on [22] select "Mode",
your target must be 22. Never invent a target. Never omit target for actions that
require an element. Never use an element ID from a previous page observation.
Use download(target), not click(target), for controls whose visible purpose is downloading
a file.

If all success criteria are already satisfied in the current page state, do not take
another browser action. Return finish.

GENERIC EXAMPLES
PAGE: [4] textbox "Search"; [5] button "Search"
Goal: search for a term
Correct: {{"action":"type","target":4,"text":"term"}}

PAGE: [8] select "Mode" options=["Basic","Advanced"]
Goal: choose Advanced
Correct: {{"action":"select","target":8,"value":"Advanced"}}

Goal has been achieved.
Correct: {{"action":"finish","result":"The requested result is visible."}}

Do not narrate. Do not explain. Output only the JSON object."""


def render_task_block(goal: str, success_criteria: list[str]) -> str:
    criteria = "\n".join(f"- {c}" for c in success_criteria) if success_criteria else "(none specified)"
    return f"TASK\n{goal}\n\nSUCCESS CRITERIA\n{criteria}"


def render_subgoal_block(current_subgoal: Optional[str], plan: list[str]) -> str:
    plan_str = "\n".join(f"{i+1}. {step}" for i, step in enumerate(plan)) if plan else "(no plan yet)"
    subgoal_str = current_subgoal or "(not yet set)"
    return f"CURRENT SUBGOAL\n{subgoal_str}\n\nPLAN\n{plan_str}"


def render_recent_actions_block(recent: list[dict]) -> str:
    if not recent:
        return "RECENT ACTIONS\n(none yet)"
    lines = ["RECENT ACTIONS"]
    for r in recent:
        verification = r.get("verification", "unknown")
        detail = ""
        result_data = r.get("result_data") or {}
        if result_data.get("suggested_filename"):
            detail = f" ({result_data['suggested_filename']})"
        elif result_data.get("extracted"):
            detail = " (extracted text)"
        lines.append(f"- step {r['step']}: {r['action']} target={r.get('target')}{detail} -> {verification}")
    return "\n".join(lines)


def render_recovery_block(recovery_level: str, recent_failures: list[str]) -> str:
    if recovery_level == "normal":
        return ""
    lines = [f"RECOVERY MODE: {recovery_level}"]
    if recent_failures:
        lines.append("Recent failures:")
        for f in recent_failures:
            lines.append(f"- {f}")
        lines.append("Reconsider your approach; do not repeat an action that already failed the same way.")
    return "\n".join(lines)


REPLAN_SYSTEM_BLOCK = """SYSTEM
You are updating the plan for a browser task that is stuck. Respond with exactly one JSON
object: {"subgoal": "<short next subgoal>", "plan": ["<step>", "..."]}. Keep the plan short
(at most 5 steps) and concrete."""


def render_replan_prompt(goal: str, success_criteria: list[str], history_summary: str,
                          blocked_reason: str) -> str:
    return (
        f"{REPLAN_SYSTEM_BLOCK}\n\n"
        f"GOAL\n{goal}\n\nSUCCESS CRITERIA\n" +
        "\n".join(f"- {c}" for c in success_criteria) +
        f"\n\nWHY BLOCKED\n{blocked_reason}\n\nHISTORY\n{history_summary}"
    )
