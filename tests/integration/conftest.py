from __future__ import annotations

import pytest

from agent.loop import AgentLoop
from tests.integration.fake_llama import ScriptedLlamaClient


@pytest.fixture
def make_agent_loop(tmp_config):
    def _make(goal: str, criteria: list[str], script: list) -> AgentLoop:
        loop = AgentLoop.create_new(tmp_config, goal, criteria)
        loop.llama = ScriptedLlamaClient(script)
        return loop
    return _make
