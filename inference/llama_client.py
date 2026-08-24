"""Local inference clients for supported browser-agent model backends.

`LlamaClient` uses llama.cpp's native `/completion` endpoint because it accepts a
`grammar` field for GBNF-constrained decoding. `OllamaClient` uses Ollama's native
structured-output JSON schema support. Both return the same `CompletionResult` so the
agent loop keeps the same parse -> Pydantic -> semantic-validation safety chain.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx

from agent.schemas import ModelDecision


class ModelUnavailableError(Exception):
    """Raised with a clean, user-facing message — never let this surface as a raw
    httpx traceback, since 'the model server isn't running' is an expected operational
    condition, not a bug."""


@dataclass
class CompletionResult:
    text: str
    prompt_tokens: Optional[int] = None
    predicted_tokens: Optional[int] = None
    prompt_ms: Optional[float] = None
    predicted_ms: Optional[float] = None
    total_latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class InferenceClient(Protocol):
    endpoint: str

    async def health_check(self) -> bool:
        ...

    async def complete(self, prompt: str, grammar: Optional[str] = None,
                        max_tokens: int = 256) -> CompletionResult:
        ...


class LlamaClient:
    def __init__(self, endpoint: str, temperature: float = 0.1, request_timeout_s: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.temperature = temperature
        self.request_timeout_s = request_timeout_s

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.endpoint}/health")
                return resp.status_code == 200
        except httpx.RequestError:
            return False

    async def complete(self, prompt: str, grammar: Optional[str] = None,
                        max_tokens: int = 256) -> CompletionResult:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "temperature": self.temperature,
            "n_predict": max_tokens,
            "cache_prompt": True,  # reuse KV cache across calls sharing this prefix
        }
        if grammar:
            payload["grammar"] = grammar

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout_s) as client:
                resp = await client.post(f"{self.endpoint}/completion", json=payload)
        except httpx.RequestError as e:
            raise ModelUnavailableError(
                f"Local model endpoint unavailable:\n{self.endpoint}\n\n"
                f"Start llama.cpp server before running the agent.\n(detail: {e})"
            ) from e
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code != 200:
            raise ModelUnavailableError(
                f"Local model endpoint returned HTTP {resp.status_code}:\n{self.endpoint}\n\n"
                f"body: {resp.text[:500]}"
            )

        data = resp.json()
        timings = data.get("timings", {}) or {}
        return CompletionResult(
            text=data.get("content", ""),
            prompt_tokens=timings.get("prompt_n"),
            predicted_tokens=timings.get("predicted_n"),
            prompt_ms=timings.get("prompt_ms"),
            predicted_ms=timings.get("predicted_ms"),
            total_latency_ms=latency_ms,
            raw=data,
        )


class OllamaClient:
    def __init__(
        self,
        endpoint: str,
        model_name: str,
        temperature: float = 0.1,
        request_timeout_s: float = 30.0,
        context_window: int = 8192,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.temperature = temperature
        self.request_timeout_s = request_timeout_s
        self.context_window = context_window
        self.schema = _model_decision_json_schema()

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.endpoint}/api/version")
                return resp.status_code == 200
        except httpx.RequestError:
            return False

    async def complete(self, prompt: str, grammar: Optional[str] = None,
                        max_tokens: int = 256) -> CompletionResult:
        action_decision = grammar is not None
        payload: dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": max_tokens,
                "num_ctx": self.context_window,
            },
        }
        # Ollama uses JSON Schema structured output, not GBNF. The agent's action
        # path passes a grammar, while recovery replan uses plain JSON.
        payload["format"] = self.schema if action_decision else "json"

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout_s) as client:
                resp = await client.post(f"{self.endpoint}/api/generate", json=payload)
        except httpx.RequestError as e:
            raise ModelUnavailableError(
                f"Local Ollama endpoint unavailable:\n{self.endpoint}\n\n"
                f"Start Ollama before running the agent.\n(detail: {e})"
            ) from e
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code != 200:
            raise ModelUnavailableError(
                f"Local Ollama endpoint returned HTTP {resp.status_code}:\n{self.endpoint}\n\n"
                f"body: {resp.text[:500]}"
            )

        data = resp.json()
        prompt_ns = data.get("prompt_eval_duration")
        predicted_ns = data.get("eval_duration")
        return CompletionResult(
            text=data.get("response", ""),
            prompt_tokens=data.get("prompt_eval_count"),
            predicted_tokens=data.get("eval_count"),
            prompt_ms=(prompt_ns / 1_000_000) if prompt_ns is not None else None,
            predicted_ms=(predicted_ns / 1_000_000) if predicted_ns is not None else None,
            total_latency_ms=latency_ms,
            raw=data,
        )


def create_inference_client(config: Any) -> InferenceClient:
    backend = config.model.backend.lower()
    if backend == "llama_cpp":
        return LlamaClient(config.model.endpoint, config.model.temperature, config.model.request_timeout_s)
    if backend == "ollama":
        return OllamaClient(
            config.model.endpoint,
            config.model.model_name,
            config.model.temperature,
            config.model.request_timeout_s,
            config.model.context_window,
        )
    raise ValueError(f"unsupported model backend: {config.model.backend!r}")


def _model_decision_json_schema() -> dict[str, Any]:
    """Ollama structured output schema matching the existing GBNF's outer contract.

    Pydantic marks fields with defaults as optional in JSON Schema, but the llama.cpp
    grammar requires the top-level action object to include target, params,
    expected_result, and confidence. Keep Ollama's constrained output equally strict at
    that layer; action-specific validation still belongs to agent.decision.
    """
    schema = ModelDecision.model_json_schema()
    schema["required"] = ["action", "target", "params", "expected_result", "confidence"]
    return schema
