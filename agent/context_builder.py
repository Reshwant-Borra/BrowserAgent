"""Deterministic prompt assembly.

Every input here comes from persisted state (`TaskRecord`/`TaskState`) or a freshly-taken
`PageObservation` — never from an in-memory chat/message list. Calling this twice with the
same arguments produces byte-identical output; that determinism is what makes "rebuild
context from persisted state after a crash" a real guarantee rather than an aspiration.

Section order (static -> semi-stable -> volatile) is fixed to maximize llama.cpp KV-cache
prefix reuse; it never changes based on content length, only the volatile tail grows/shrinks.
"""
from __future__ import annotations

from agent.schemas import DecisionValidationError, ValidationErrorKind
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


def build_contract_repair_prompt(
    task: TaskRecord,
    observation: PageObservation,
    raw_response: str,
    error: DecisionValidationError,
    max_page_chars: int,
    max_visible_text_items: int,
) -> str:
    lines = [
        prompt_templates.SYSTEM_BLOCK,
        prompt_templates.render_task_block(task.goal, task.success_criteria),
        "CONTRACT REPAIR",
        "Your previous response is invalid.",
        f"Error: {error.kind.value}: {error.message}",
        f"Previous response: {raw_response[:500]}",
        _valid_targets_hint(error, observation),
        "Return only one corrected JSON action using the current page IDs.",
        observation.render_compact(max_page_chars, max_visible_text_items),
    ]
    return "\n\n".join(part for part in lines if part)


def _valid_targets_hint(error: DecisionValidationError, observation: PageObservation) -> str:
    if error.kind == ValidationErrorKind.MISSING_TARGET:
        raw = error.message.lower()
        if "select" in raw:
            roles = {"select", "combobox"}
        elif "type" in raw:
            roles = {"textbox"}
        else:
            roles = {"button", "link", "checkbox", "radio", "tab", "menuitem", "select", "combobox", "textbox"}
    elif error.kind == ValidationErrorKind.TARGET_TYPE_MISMATCH:
        raw = error.message.lower()
        if "select" in raw:
            roles = {"select", "combobox"}
        elif "type" in raw:
            roles = {"textbox"}
        else:
            roles = {e.role for e in observation.elements}
    else:
        roles = {e.role for e in observation.elements}

    rows = [e for e in observation.elements if e.role in roles and not e.disabled]
    if not rows:
        return "No valid element targets of the required type are visible on the current page."
    rendered = ["Current valid targets:"]
    for e in rows:
        suffix = f" options={e.options}" if e.options else ""
        rendered.append(f'[{e.id}] {e.role} "{e.name}"{suffix}')
    return "\n".join(rendered)
