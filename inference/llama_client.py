"""Thin client for a locally-running llama.cpp server (`llama-server`).

Uses the native `/completion` endpoint (not the OpenAI-compatible shim) specifically
because it accepts a `grammar` field for GBNF-constrained decoding directly, and its
response includes a `timings` block we use for the input/output token + latency metrics
required by ARCHITECTURE.md's instrumentation section.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


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
