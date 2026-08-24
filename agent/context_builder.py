"""Deterministic prompt assembly.

Every input here comes from persisted state (`TaskRecord`/`TaskState`) or a freshly-taken
`PageObservation` — never from an in-memory chat/message list. Calling this twice with the
same arguments produces byte-identical output; that determinism is what makes "rebuild
context from persisted state after a crash" a real guarantee rather than an aspiration.

Section order (static -> semi-stable -> volatile) is fixed to maximize llama.cpp KV-cache
prefix reuse; it never changes based on content length, only the volatile tail grows/shrinks.
"""
from __future__ import annotations

from browser.page_model import PageObservation
from inference import prompt as prompt_templates
from memory.models import TaskRecord, TaskState


def build_prompt(task: TaskRecord, state: TaskState, observation: PageObservation,
                  max_page_chars: int, max_visible_text_items: int,
                  recent_failures: list[str] | None = None) -> str:
    sections = [
        prompt_templates.SYSTEM_BLOCK,
        prompt_templates.render_task_block(task.goal, task.success_criteria),
        prompt_templates.render_subgoal_block(state.current_subgoal, state.plan),
        prompt_templates.render_recent_actions_block(state.recent_actions),
    ]

    recovery_block = prompt_templates.render_recovery_block(state.recovery_level, recent_failures or [])
    if recovery_block:
        sections.append(recovery_block)

    sections.append(observation.render_compact(max_page_chars, max_visible_text_items))

    return "\n\n".join(sections)


def build_replan_prompt(task: TaskRecord, state: TaskState) -> str:
    history_lines = [
        f"step {r['step']}: {r['action']} target={r.get('target')} -> {r.get('verification', 'unknown')}"
        for r in state.recent_actions
    ]
    return prompt_templates.render_replan_prompt(
        goal=task.goal,
        success_criteria=task.success_criteria,
        history_summary="\n".join(history_lines) or "(no actions yet)",
        blocked_reason=state.blocked_reason or "(unspecified)",
    )
