"""Deterministic unit tests for agent/ranking.py (Phase 3 section 9.2's RankRequest/RankResult
contract). No live Ollama — a scripted fake client stands in, mirroring
tests/integration/fake_llama.py's ScriptedLlamaClient pattern.
"""
from __future__ import annotations

import json

import pytest

from agent import ranking
from agent.workspace_models import WorkspaceEntity
from inference.llama_client import CompletionResult


def _entity(id_, name=None, **attrs) -> WorkspaceEntity:
    return WorkspaceEntity(id=id_, entity_type="candidate", name=name, attributes=attrs)


class _RaisesIfCalledClient:
    endpoint = "fake://never"

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
        raise AssertionError("deterministic-only ranking must never call the model")

    async def health_check(self):
        return True


class _ScriptedRankClient:
    def __init__(self, response: dict):
        self.endpoint = "fake://rank"
        self.response = response
        self.calls = 0

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
        self.calls += 1
        return CompletionResult(text=json.dumps(self.response), total_latency_ms=1.0)

    async def health_check(self):
        return True


class _RaisingTextClient:
    def __init__(self, text: str):
        self.endpoint = "fake://bad"
        self.text = text

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
        return CompletionResult(text=self.text, total_latency_ms=1.0)

    async def health_check(self):
        return True


async def test_deterministic_single_numeric_preference_never_calls_model():
    entities = [_entity("a", price=30), _entity("b", price=10), _entity("c", price=20)]
    request = ranking.RankRequest(
        entity_ids=["a", "b", "c"], objective="cheapest widgets",
        numeric_preferences=[ranking.NumericPreference(field="price", direction="min")], k=2,
    )
    result = await ranking.rank_candidates(_RaisesIfCalledClient(), request, entities)
    assert result.ranked_entity_ids == ["b", "c"]


async def test_semantic_ranking_calls_model_and_validates_ids():
    entities = [_entity("a"), _entity("b"), _entity("c")]
    client = _ScriptedRankClient({
        "ranked_entity_ids": ["c", "a"],
        "rationale_by_entity": {"c": "best fit", "a": "second"},
        "missing_information": [],
    })
    request = ranking.RankRequest(entity_ids=["a", "b", "c"], objective="best overall", k=2)
    result = await ranking.rank_candidates(client, request, entities)
    assert result.ranked_entity_ids == ["c", "a"]
    assert result.rationale_by_entity == {"c": "best fit", "a": "second"}
    assert client.calls == 1


async def test_hallucinated_ids_are_dropped_not_trusted():
    entities = [_entity("a"), _entity("b")]
    client = _ScriptedRankClient({
        "ranked_entity_ids": ["nonexistent", "a", "also_fake"],
        "rationale_by_entity": {}, "missing_information": [],
    })
    request = ranking.RankRequest(entity_ids=["a", "b"], objective="anything", k=2)
    result = await ranking.rank_candidates(client, request, entities)
    assert result.ranked_entity_ids == ["a"]


async def test_all_hallucinated_ids_raises():
    entities = [_entity("a"), _entity("b")]
    client = _ScriptedRankClient({
        "ranked_entity_ids": ["nonexistent"], "rationale_by_entity": {}, "missing_information": [],
    })
    request = ranking.RankRequest(entity_ids=["a", "b"], objective="anything", k=1)
    with pytest.raises(ranking.RankingOutputError):
        await ranking.rank_candidates(client, request, entities)


async def test_result_truncated_and_deduped_to_k():
    entities = [_entity("a"), _entity("b"), _entity("c")]
    client = _ScriptedRankClient({
        "ranked_entity_ids": ["a", "a", "b", "c"], "rationale_by_entity": {}, "missing_information": [],
    })
    request = ranking.RankRequest(entity_ids=["a", "b", "c"], objective="anything", k=2)
    result = await ranking.rank_candidates(client, request, entities)
    assert result.ranked_entity_ids == ["a", "b"]


async def test_malformed_json_raises_ranking_output_error():
    entities = [_entity("a")]
    client = _RaisingTextClient("not json at all")
    request = ranking.RankRequest(entity_ids=["a"], objective="anything", k=1)
    with pytest.raises(ranking.RankingOutputError):
        await ranking.rank_candidates(client, request, entities)


async def test_schema_invalid_json_raises_ranking_output_error():
    entities = [_entity("a")]
    client = _RaisingTextClient(json.dumps({"totally": "wrong shape"}))
    request = ranking.RankRequest(entity_ids=["a"], objective="anything", k=1)
    with pytest.raises(ranking.RankingOutputError):
        await ranking.rank_candidates(client, request, entities)


async def test_no_matching_candidate_entities_raises_without_a_call():
    entities = [_entity("z")]
    request = ranking.RankRequest(entity_ids=["a", "b"], objective="anything", k=1)
    with pytest.raises(ranking.RankingOutputError):
        await ranking.rank_candidates(_RaisesIfCalledClient(), request, entities)
