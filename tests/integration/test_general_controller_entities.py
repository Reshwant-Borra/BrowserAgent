"""Phase 3 (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18:
"Generic Entity Collection, Evidence, and Top-N Completion") integration tests. Same
deterministic-scripted-model approach as tests/integration/test_general_controller.py — real
Playwright + event store + WorkspaceStore + child AgentLoop pipeline, no live Ollama. Proves
the wiring (structured findings -> workspace entities -> deterministic top-k selection ->
exact-k final result) end to end; live-model semantic-ranking accuracy across the five holdout
domains is covered separately by benchmarks/general_agent/run_phase3_entities.py.
"""
from __future__ import annotations

import json
import re

import pytest

from agent.config import AppConfig
from agent.controller import GeneralAgentController
from inference.llama_client import CompletionResult
from tests.integration.fake_llama import ScriptedLlamaClient, decision


class _SequencedSchemaClient:
    """Same pattern as test_general_controller.py's own fake: one queue per schema title.
    Additionally answers a "RankResult" schema by reading the candidate ids straight out of
    the prompt (agent/ranking.py always renders each candidate as `[ent_xxxxx]`) rather than
    needing the test to predict the controller's internally-generated entity ids up front."""

    def __init__(self, responses_by_title: dict[str, list], rank_k: int = 0):
        self._queues = {k: list(v) for k, v in responses_by_title.items()}
        self.endpoint = "fake://controller"
        self.calls: list[str] = []
        self._rank_k = rank_k

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        if title == "RankResult":
            ids = re.findall(r"\[(ent_[0-9a-f]+)\]", prompt)
            selected = ids[: self._rank_k]
            return CompletionResult(text=json.dumps({
                "ranked_entity_ids": selected,
                "rationale_by_entity": {i: "cheapest" for i in selected},
                "missing_information": [],
            }), total_latency_ms=1.0)
        queue = self._queues.get(title)
        if not queue:
            raise AssertionError(f"schema {title!r} ran out of scripted responses (prompt: {prompt[:150]!r})")
        raw = queue.pop(0)
        return CompletionResult(text=json.dumps(raw), total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def _config(tmp_config: AppConfig, **agent_overrides) -> AppConfig:
    for key, value in agent_overrides.items():
        setattr(tmp_config.agent, key, value)
    return tmp_config


def _candidate_finish(name: str, price: str, url: str):
    def factory_step():
        return [
            decision("open_url", params={"url": url}),
            decision("finish", params={
                "result": f"recorded {name}",
                "structured_result": {
                    "summary": f"page about {name}",
                    "findings": [
                        {"field": "item_name", "value": name, "evidence": f"{name} listed"},
                        {"field": "price_usd", "value": price, "evidence": f"${price} listed"},
                    ],
                },
            }),
        ]
    return factory_step


def _multi_candidate_child_factory(fixture_site_url: str, candidates: list[tuple[str, str]]):
    """Each call returns a fresh ScriptedLlamaClient for the next candidate in order —
    mirrors test_general_controller.py's own _open_and_finish_child_factory."""
    calls = {"n": 0}

    def factory():
        idx = calls["n"]
        calls["n"] += 1
        name, price = candidates[idx]
        steps = _candidate_finish(name, price, f"{fixture_site_url}/index.html")()
        return ScriptedLlamaClient(steps)
    return factory


async def test_structured_finish_creates_entity_with_evidence(tmp_config, fixture_site_url):
    config = _config(tmp_config, completion_check_after_subgoal=False, max_steps_per_subgoal=10)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "record Widget A's price", "plan": ["record Widget A's price"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    child_factory = _multi_candidate_child_factory(fixture_site_url, [("Widget A", "19.99")])
    controller = GeneralAgentController.create_new(
        config, "record widget prices", [], llama_client=planner_client, child_llama_client_factory=child_factory,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert len(workspace.entities) == 1
        entity = workspace.entities[0]
        assert entity.entity_type == "candidate"
        assert entity.name == "Widget A"
        assert entity.attributes == {"item_name": "Widget A", "price_usd": "19.99"}
        assert entity.status == "active"

        entity_evidence = [e for e in workspace.evidence if e.entity_id == entity.id]
        assert len(entity_evidence) == 2
        assert {e.field_key for e in entity_evidence} == {"item_name", "price_usd"}
    finally:
        controller.close()


async def test_top_k_completion_selects_exactly_k_and_rejects_the_rest(tmp_config, fixture_site_url):
    config = _config(tmp_config, completion_check_after_subgoal=True, max_steps_per_subgoal=10,
                      planner_max_subgoals=5)
    candidates = [("Widget A", "30.00"), ("Widget B", "10.00"), ("Widget C", "20.00")]
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "record Widget A's price",
             "plan": ["record Widget A's price", "record Widget B's price", "record Widget C's price"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": False, "missing_requirements": ["more candidates needed"],
             "unsupported_claims": [], "next_recommendation": "continue"},
            {"satisfied": False, "missing_requirements": ["more candidates needed"],
             "unsupported_claims": [], "next_recommendation": "continue"},
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    }, rank_k=2)
    child_factory = _multi_candidate_child_factory(fixture_site_url, candidates)
    controller = GeneralAgentController.create_new(
        config, "Find the 2 cheapest widgets and report them.", [],
        llama_client=planner_client, child_llama_client_factory=child_factory,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"

        workspace = controller.workspace_store.load(controller.control_task_id)
        assert len(workspace.entities) == 3  # all three candidates collected, none invented
        selected = [e for e in workspace.entities if e.status == "selected"]
        rejected = [e for e in workspace.entities if e.status == "rejected"]
        assert len(selected) == 2
        assert len(rejected) == 1

        # WorkspaceStore orders entities by id (random per-run, not insertion order), so which
        # 2 of the 3 real widgets the fake ranker's "first k in the prompt" picks varies run to
        # run — the invariant under test is that every selected/rejected name is one of the
        # three real candidates actually collected, never an invented fourth one.
        all_names = {"Widget A", "Widget B", "Widget C"}
        assert {e.name for e in selected} | {e.name for e in rejected} == all_names
        assert {e.name for e in selected}.issubset(all_names)
    finally:
        controller.close()


async def test_top_k_not_triggered_when_not_enough_candidates_yet(tmp_config, fixture_site_url):
    """Goal asks for "3 cheapest" but only 1 candidate is ever collected before the plan is
    exhausted and the model itself reports satisfied=True — _maybe_select_top_k_entities must
    not fabricate a 3-item report out of 1 real entity; it should fall back to the ordinary
    completion_claim text instead (never "no unsupported final entity" violated)."""
    config = _config(tmp_config, completion_check_after_subgoal=False, max_steps_per_subgoal=10)
    planner_client = _SequencedSchemaClient({
        "ControllerDecision": [
            {"decision": "start_subgoal", "reason_code": "initial_plan",
             "active_subgoal": "record Widget A's price", "plan": ["record Widget A's price"],
             "resource_refs": [], "clarification_question": None, "completion_claim": None},
        ],
        "CompletionEvaluation": [
            {"satisfied": True, "missing_requirements": [], "unsupported_claims": [],
             "next_recommendation": "finish"},
        ],
    })
    child_factory = _multi_candidate_child_factory(fixture_site_url, [("Widget A", "19.99")])
    controller = GeneralAgentController.create_new(
        config, "Find the 3 cheapest widgets and report them.", [],
        llama_client=planner_client, child_llama_client_factory=child_factory,
    )
    try:
        state = await controller.run()
        assert state.status == "completed"
        workspace = controller.workspace_store.load(controller.control_task_id)
        assert all(e.status == "active" for e in workspace.entities)  # never force-selected
    finally:
        controller.close()
