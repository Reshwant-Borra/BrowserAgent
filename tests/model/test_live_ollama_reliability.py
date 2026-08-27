from __future__ import annotations

import json

import pytest

from agent.config import load_config
from agent.decision import parse_model_output
from inference.llama_client import create_inference_client

pytestmark = pytest.mark.model


@pytest.fixture
async def live_ollama_config():
    config = load_config()
    if config.model.backend.lower() != "ollama":
        pytest.skip("live Ollama reliability diagnostic only runs for the ollama backend")
    client = create_inference_client(config)
    if not await client.health_check():
        pytest.skip(f"no live Ollama server reachable at {client.endpoint}")
    return config


async def test_live_ollama_repeated_structured_calls(live_ollama_config):
    config = live_ollama_config
    client = create_inference_client(config)
    grammar_marker = "structured action schema"
    prompt = (
        "Choose the only valid browser action for this state.\n"
        "Goal: finish because the answer is already known.\n"
        "Current page text: Ready.\n"
        "Return a finish action with result set to ok."
    )

    rows = []
    for _ in range(5):
        completion = await client.complete(prompt, grammar=grammar_marker, max_tokens=64)
        decision = parse_model_output(completion.text)
        rows.append({
            "action": decision.action.value,
            "attempts": completion.attempt_count,
            "request_id": completion.request_id,
        })

    assert all(row["action"] == "finish" for row in rows), json.dumps(rows, indent=2)
    assert all(row["attempts"] >= 1 for row in rows)
