"""V2 test scaffolding.

`ScriptedClient` stands in for Ollama: it returns a fixed sequence of decisions (or lets a
callable inspect the prompt and answer), so every V2 integration test is deterministic and
runs without a model server. The browser, the page, the observer, the executor and the
verifier are all real — only inference is faked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from inference.llama_client import CompletionResult
from agent_v2.agent import BrowserAgentV2, LoopLimits
from agent_v2.browser_ops import BrowserSession
from agent_v2.memory import MemoryStore
from browser.playwright_backend import PlaywrightBackend


class ScriptedClient:
    """Each script entry is either a dict (returned verbatim as the decision JSON) or a
    callable taking the prompt and returning a dict. Prompts are recorded so tests can
    assert on what the model was and was not shown."""

    endpoint = "scripted://"

    def __init__(self, script: list, extraction: dict | None = None):
        self.script = list(script)
        self.extraction = extraction
        self.prompts: list[str] = []
        #: Decision prompts only — the end-of-task memory-extraction call is a different
        #: prompt entirely and would otherwise be `prompts[-1]` for every memory test.
        self.decision_prompts: list[str] = []
        self.calls = 0

    async def health_check(self) -> bool:
        return True

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None):
        self.prompts.append(prompt)
        self.calls += 1
        if json_schema is not None and "memories" in (json_schema.get("properties") or {}):
            payload = self.extraction if self.extraction is not None else {"memories": []}
            return CompletionResult(text=json.dumps(payload), prompt_tokens=10, predicted_tokens=5)
        self.decision_prompts.append(prompt)
        if not self.script:
            payload = {"action": "finish", "answer": "SCRIPT EXHAUSTED", "reason": "script empty"}
        else:
            entry = self.script.pop(0)
            payload = entry(prompt) if callable(entry) else entry
        return CompletionResult(text=json.dumps(payload), prompt_tokens=100, predicted_tokens=20)


@pytest.fixture
async def backend(tmp_path):
    backend = PlaywrightBackend(
        profile_dir=tmp_path / "profile",
        headless=True,
        action_timeout_ms=5000,
        max_page_chars=4000,
        max_visible_text_items=20,
        mode="launch",
    )
    await backend.start()
    yield backend
    await backend.close()


@pytest.fixture
def make_agent(tmp_path):
    def _make(backend, script, *, memory: MemoryStore | None = None, extraction=None,
              limits: LoopLimits | None = None, approval=None, takeover=None,
              task_dir: Path | None = None) -> tuple[BrowserAgentV2, ScriptedClient]:
        client = ScriptedClient(script, extraction=extraction)
        agent = BrowserAgentV2(
            session=BrowserSession(backend),
            client=client,
            memory=memory,
            task_dir=task_dir if task_dir is not None else tmp_path / "task",
            limits=limits or LoopLimits(max_steps=12),
            approval=approval,
            takeover=takeover,
        )
        return agent, client
    return _make


@pytest.fixture
def memory_store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    yield store
    store.close()
