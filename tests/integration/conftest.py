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


@pytest.fixture
def make_agent_loop_at_url(tmp_config):
    """Like `make_agent_loop`, but the browser is already on `explicit_target_url` before the
    model's first decision — mirroring how a batch/research work item actually starts (see
    batch/orchestrator.py's `_runtime_policy`/PlaywrightBackend's explicit_target_url), instead
    of always starting from about:blank and needing the model's first move to be a navigating
    `open_url`. That distinction matters for `_handle_finish` (agent/loop.py): every
    single-site test elsewhere gets a "free" prior verified action from that initial open_url,
    which masks any bug in the fallback terminal-evidence gate that only applies when there is
    truly no prior passing action — exactly the situation a batch work item's child task can be
    in on its very first step."""
    def _make(goal: str, criteria: list[str], script: list, explicit_target_url: str) -> AgentLoop:
        loop = AgentLoop.create_new(tmp_config, goal, criteria, explicit_target_url=explicit_target_url)
        loop.llama = ScriptedLlamaClient(script)
        return loop
    return _make
