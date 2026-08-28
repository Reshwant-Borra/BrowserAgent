"""Deterministic prompt assembly.

Every input here comes from persisted state (`TaskRecord`/`TaskState`) or a freshly-taken
`PageObservation` — never from an in-memory chat/message list. Calling this twice with the
same arguments produces byte-identical output; that determinism is what makes "rebuild
context from persisted state after a crash" a real guarantee rather than an aspiration.

Section order (static -> semi-stable -> volatile) is fixed to maximize llama.cpp KV-cache
prefix reuse; it never changes based on content length, only the volatile tail grows/shrinks.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from agent.config import ContextConfig
from agent.schemas import DecisionValidationError, ValidationErrorKind
from agent.token_budget import PromptBlock, count_tokens, trim_to_token_budget
from agent.workspace_models import WorkspaceView
from browser.page_model import PageObservation
from inference import prompt as prompt_templates
from memory.event_store import EventStore
from memory.models import TaskRecord, TaskState
from memory.task_memory import (
    ActiveFact,
    MemoryRecord,
    RetrievalMetrics,
    TaskMemoryStore,
    build_retrieval_queries,
    normalize_fact_key,
)


@dataclass(frozen=True)
class ContextPackage:
    prompt: str
    block_tokens: dict[str, int]
    block_chars: dict[str, int]
    retrieved_memory_count: int
    running_summary_tokens: int
    total_estimated_tokens: int
    retrieval_query_terms: list[str]
    candidate_memory_ids: list[int]
    selected_memory_ids: list[int]
    selected_memory_kinds: list[str]
    selected_memory_source_event_ids: list[int]
    selected_memory_tokens: int
    active_facts: list[ActiveFact]
    active_fact_tokens: int
    timing_ms: dict[str, float]


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


def build_tiered_context(
    task: TaskRecord,
    state: TaskState,
    observation: PageObservation,
    context_config: ContextConfig,
    max_page_chars: int,
    max_visible_text_items: int,
    event_store: EventStore,
    recent_failures: list[str] | None = None,
) -> ContextPackage:
    """Build the Phase 4 bounded working set from persisted state and a fresh page.

    This function may refresh derived summary/memory tables, but it never mutates raw task
    history. The returned prompt is fully reconstructable from SQLite events plus the current
    page observation.
    """
    memory_store = TaskMemoryStore(event_store)

    timing: dict[str, float] = {}
    started = time.monotonic()
    inserted_memories = memory_store.ingest_new_events(task.id)
    timing["memory_ingest_ms"] = (time.monotonic() - started) * 1000

    started = time.monotonic()
    events = event_store.all_events(task.id) if context_config.enable_running_summary else []
    timing["event_load_ms"] = (time.monotonic() - started) * 1000

    summary = None
    started = time.monotonic()
    if context_config.enable_running_summary:
        summary = memory_store.compact_if_needed(
            task.id,
            events,
            keep_last_steps=context_config.recent_actions,
            summary_token_budget=context_config.summary_tokens,
            rebuild_interval=context_config.summary_rebuild_interval,
        )
    else:
        summary = memory_store.get_summary(task.id)
    timing["summary_update_ms"] = (time.monotonic() - started) * 1000

    recent = state.recent_actions[-context_config.recent_actions:]

    active_facts: list[ActiveFact] = []
    if context_config.enable_active_facts:
        active_facts = memory_store.active_facts(
            task.id,
            observation,
            token_budget=context_config.active_fact_tokens,
        )
    active_fact_block = prompt_templates.render_active_facts_block(
        [_active_fact_to_prompt_dict(fact, observation) for fact in active_facts]
    )
    active_fact_tokens = count_tokens(active_fact_block) if active_facts else 0

    started = time.monotonic()
    queries = build_retrieval_queries(
        task.goal,
        task.success_criteria,
        state.current_subgoal,
        observation,
        active_facts,
    )
    timing["retrieval_query_build_ms"] = (time.monotonic() - started) * 1000

    retrieved = []
    retrieval_metrics = RetrievalMetrics([], [], [], [], [], 0, 0.0)
    if context_config.enable_memory_retrieval:
        search_result = memory_store.search_queries(
            task.id,
            queries,
            top_k=context_config.retrieved_memory_top_k,
            token_budget=context_config.retrieved_memory_tokens,
        )
        retrieved = search_result.records
        retrieval_metrics = search_result.metrics
    timing["fts_search_ms"] = retrieval_metrics.retrieval_ms

    blocks = [
        PromptBlock("static_prefix", prompt_templates.SYSTEM_BLOCK),
        PromptBlock("task_state", "\n\n".join([
            prompt_templates.render_task_block(task.goal, task.success_criteria),
            prompt_templates.render_subgoal_block(state.current_subgoal, state.plan),
        ])),
    ]
    if summary:
        blocks.append(PromptBlock("running_summary", trim_to_token_budget(
            prompt_templates.render_running_summary_block(summary.summary),
            context_config.summary_tokens,
        )))
    if retrieved:
        blocks.append(PromptBlock("retrieved_memory", prompt_templates.render_retrieved_memory_block(
            [_memory_to_prompt_dict(m) for m in retrieved]
        )))
    blocks.append(PromptBlock("recent_window", trim_to_token_budget(
        prompt_templates.render_recent_actions_block(recent),
        context_config.recent_window_tokens,
    )))

    recovery_block = prompt_templates.render_recovery_block(state.recovery_level, recent_failures or [])
    if recovery_block:
        blocks.append(PromptBlock("recovery", recovery_block))
    if active_facts:
        blocks.append(PromptBlock("active_facts", trim_to_token_budget(
            active_fact_block,
            context_config.active_fact_tokens,
        )))
    non_page_tokens = sum(block.tokens for block in blocks)
    page_budget = min(context_config.page_tokens, max(0, context_config.max_total_tokens - non_page_tokens))
    page = trim_to_token_budget(
        observation.render_compact(max_page_chars, max_visible_text_items),
        page_budget,
    )
    blocks.append(PromptBlock("page", page))

    started = time.monotonic()
    prompt = "\n\n".join(block.text for block in blocks if block.text)
    total = count_tokens(prompt)
    timing["context_render_ms"] = (time.monotonic() - started) * 1000
    timing["derived_memory_inserted"] = float(inserted_memories)
    return ContextPackage(
        prompt=prompt,
        block_tokens={block.name: block.tokens for block in blocks},
        block_chars={block.name: len(block.text) for block in blocks},
        retrieved_memory_count=len(retrieved),
        running_summary_tokens=count_tokens(summary.summary) if summary else 0,
        total_estimated_tokens=total,
        retrieval_query_terms=retrieval_metrics.query_terms,
        candidate_memory_ids=retrieval_metrics.candidate_memory_ids,
        selected_memory_ids=retrieval_metrics.selected_memory_ids,
        selected_memory_kinds=retrieval_metrics.selected_memory_kinds,
        selected_memory_source_event_ids=retrieval_metrics.selected_memory_source_event_ids,
        selected_memory_tokens=retrieval_metrics.selected_memory_tokens,
        active_facts=active_facts,
        active_fact_tokens=active_fact_tokens,
        timing_ms=timing,
    )


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


def build_workspace_summary(workspace: WorkspaceView, max_entities: int, max_evidence: int) -> str:
    """The general controller's "relevant workspace slice" (BrowserAgent_General_Autonomous_
    Agent_Architecture_REVISED.pdf, section 8's agent/context_builder.py entry, config's
    max_workspace_entities_in_context / max_workspace_evidence_in_context). Deliberately not
    wired into build_tiered_context/build_prompt above — those remain AgentLoop's unmodified
    click-level context; this is consumed only by agent/planner.py's controller-level prompts
    (see inference/prompt.py's render_workspace_block docstring for why the split exists)."""
    entities = workspace.entities[:max_entities]
    evidence = workspace.evidence[:max_evidence]
    # Keys prefixed "_" are controller-internal bookkeeping (e.g. agent/controller.py's hub
    # URL tracking), never shown to the model — same convention as a private attribute.
    visible_facts = {k: v for k, v in workspace.facts.items() if not k.startswith("_")}
    return prompt_templates.render_workspace_block(
        [e.model_dump() for e in entities],
        [e.model_dump() for e in evidence],
        workspace.open_questions,
        workspace.completion_requirements,
        visible_facts,
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


def _memory_to_prompt_dict(memory: MemoryRecord) -> dict:
    return {
        "kind": memory.kind,
        "content": memory.content,
        "source_event_id": memory.source_event_id,
        "confidence": memory.confidence,
    }


def _active_fact_to_prompt_dict(fact: ActiveFact, observation: PageObservation | None = None) -> dict:
    data = {
        "kind": fact.kind,
        "key": fact.key,
        "value": fact.value,
        "source_event_id": fact.source_event_id,
        "source_text": fact.source_text,
        "confidence": fact.confidence,
    }
    if observation is not None:
        match = _matching_control(fact, observation)
        if match is not None:
            data["target_id"] = match.id
            data["target_name"] = match.name
    return data


def _matching_control(fact: ActiveFact, observation: PageObservation):
    fact_key = normalize_fact_key(fact.key)
    for element in observation.elements:
        element_name = normalize_fact_key(element.name)
        if fact_key == element_name or fact_key in set(element_name.split()):
            return element
    return None
