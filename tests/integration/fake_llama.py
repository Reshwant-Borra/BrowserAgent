"""A scripted stand-in for LlamaClient used by integration tests that need to exercise the
real Playwright + event-store + verifier + recovery pipeline deterministically, without a
live llama.cpp server. Only tests/model/* (marker `model`) talk to a real model — everything
here is testing the deterministic machinery *around* the model call, which is exactly what
Phases 1-3 are about proving.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from inference.llama_client import CompletionResult


class ScriptedLlamaClient:
    """Takes a list of JSON-string decisions (or callables producing one, for
    context-dependent scripts) and returns them in order on successive `complete()` calls.
    Raises if the script runs out — a test that needs more steps than it scripted is a bug
    in the test, not something to silently paper over."""

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self._index = 0
        self.endpoint = "scripted://fake"
        self.calls: list[str] = []

    async def complete(self, prompt: str, grammar: Optional[str] = None, max_tokens: int = 256) -> CompletionResult:
        self.calls.append(prompt)
        if self._index >= len(self._script):
            raise AssertionError(
                f"ScriptedLlamaClient ran out of scripted responses after {self._index} calls"
            )
        item = self._script[self._index]
        self._index += 1
        text = item(prompt) if callable(item) else item
        if not isinstance(text, str):
            text = json.dumps(text)
        return CompletionResult(text=text, prompt_tokens=len(prompt) // 4, predicted_tokens=len(text) // 4,
                                 total_latency_ms=1.0)

    async def health_check(self) -> bool:
        return True


def decision(action: str, target: Optional[int] = None, params: Optional[dict] = None,
             expected_result: Optional[dict] = None, confidence: float = 0.9) -> str:
    return json.dumps({
        "action": action, "target": target, "params": params or {},
        "expected_result": expected_result or {}, "confidence": confidence,
    })
