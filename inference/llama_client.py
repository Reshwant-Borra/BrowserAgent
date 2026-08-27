"""Local inference clients for supported browser-agent model backends."""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

import httpx
from pydantic import TypeAdapter

from agent.schemas import ModelAction


class InferenceFailureCategory(str, Enum):
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    READ_TIMEOUT = "READ_TIMEOUT"
    TOTAL_REQUEST_TIMEOUT = "TOTAL_REQUEST_TIMEOUT"
    OLLAMA_HTTP_ERROR = "OLLAMA_HTTP_ERROR"
    CONNECTION_RESET = "CONNECTION_RESET"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    STRUCTURED_OUTPUT_FAILURE = "STRUCTURED_OUTPUT_FAILURE"
    UNKNOWN_INFERENCE_FAILURE = "UNKNOWN_INFERENCE_FAILURE"


RETRYABLE_INFERENCE_CATEGORIES = {
    InferenceFailureCategory.CONNECT_TIMEOUT,
    InferenceFailureCategory.READ_TIMEOUT,
    InferenceFailureCategory.CONNECTION_RESET,
    InferenceFailureCategory.SERVICE_UNAVAILABLE,
}


class ModelUnavailableError(Exception):
    """Clean operational inference failure with machine-readable diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        category: InferenceFailureCategory = InferenceFailureCategory.UNKNOWN_INFERENCE_FAILURE,
        request_id: str | None = None,
        retryable: bool = False,
        attempts: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.request_id = request_id
        self.retryable = retryable
        self.attempts = attempts or []


@dataclass
class CompletionResult:
    text: str
    prompt_tokens: Optional[int] = None
    predicted_tokens: Optional[int] = None
    prompt_ms: Optional[float] = None
    predicted_ms: Optional[float] = None
    total_latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    attempt_count: int = 1
    inference_attempts: list[dict[str, Any]] = field(default_factory=list)


class InferenceClient(Protocol):
    endpoint: str

    async def health_check(self) -> bool:
        ...

    async def complete(
        self,
        prompt: str,
        grammar: Optional[str] = None,
        max_tokens: int = 256,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> CompletionResult:
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

    async def complete(
        self,
        prompt: str,
        grammar: Optional[str] = None,
        max_tokens: int = 256,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> CompletionResult:
        # llama.cpp's structured-output path here is GBNF (`grammar`), not JSON Schema.
        # `json_schema` is accepted for interface parity with OllamaClient (router/llm_router.py
        # picks whichever backend is configured) but is not schema-enforced on this backend;
        # callers must validate the returned JSON themselves, same as any non-grammar completion.
        payload: dict[str, Any] = {
            "prompt": prompt,
            "temperature": self.temperature,
            "n_predict": max_tokens,
            "cache_prompt": True,
        }
        if grammar:
            payload["grammar"] = grammar

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout_s) as client:
                resp = await client.post(f"{self.endpoint}/completion", json=payload)
        except httpx.RequestError as exc:
            category = _classify_httpx_error(exc)
            raise ModelUnavailableError(
                f"Local model inference failed [{category.value}]:\n{self.endpoint}\n\n"
                f"detail: {exc}",
                category=category,
                retryable=category in RETRYABLE_INFERENCE_CATEGORIES,
            ) from exc
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code != 200:
            raise ModelUnavailableError(
                f"Local model endpoint returned HTTP {resp.status_code}:\n{self.endpoint}\n\n"
                f"body: {resp.text[:500]}",
                category=InferenceFailureCategory.OLLAMA_HTTP_ERROR,
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
    active_requests = 0

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        temperature: float = 0.1,
        request_timeout_s: float = 30.0,
        context_window: int = 8192,
        request_connect_timeout_s: float = 10.0,
        request_write_timeout_s: float = 10.0,
        request_pool_timeout_s: float = 10.0,
        max_inference_attempts: int = 2,
        retry_backoff_s: float = 0.5,
        keep_alive: str = "5m",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.temperature = temperature
        self.request_timeout_s = request_timeout_s
        self.context_window = context_window
        self.request_connect_timeout_s = request_connect_timeout_s
        self.request_write_timeout_s = request_write_timeout_s
        self.request_pool_timeout_s = request_pool_timeout_s
        self.max_inference_attempts = max(1, int(max_inference_attempts))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.keep_alive = keep_alive
        self.transport = transport
        self.schema = _model_decision_json_schema()

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as client:
                resp = await client.get(f"{self.endpoint}/api/version")
                return resp.status_code == 200
        except httpx.RequestError:
            return False

    async def complete(
        self,
        prompt: str,
        grammar: Optional[str] = None,
        max_tokens: int = 256,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> CompletionResult:
        action_decision = grammar is not None
        request_id = hashlib.sha256(
            f"{time.time_ns()}:{self.model_name}:{len(prompt)}".encode("utf-8")
        ).hexdigest()[:16]
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
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
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        # `json_schema` (e.g. router/llm_router.py's RouterDecision schema) takes priority over
        # the fixed per-instance action schema, so callers other than the agent decision loop
        # can reuse this same client/retry/diagnostics machinery for other structured calls.
        if json_schema is not None:
            payload["format"] = json_schema
        else:
            payload["format"] = self.schema if action_decision else "json"

        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.max_inference_attempts + 1):
            try:
                data, diagnostics = await self._post_generate(
                    payload,
                    request_id=request_id,
                    prompt=prompt,
                    prompt_hash=prompt_hash,
                    schema_type="custom_json_schema" if json_schema is not None else ("action_json_schema" if action_decision else "json"),
                    attempt=attempt,
                )
                attempts.append(diagnostics)
                break
            except ModelUnavailableError as exc:
                attempts.extend(exc.attempts)
                if attempt >= self.max_inference_attempts or not exc.retryable:
                    exc.attempts = attempts
                    raise
                await self._health_check_after_failure(exc.category)
                await asyncio.sleep(self.retry_backoff_s * attempt)
        else:
            raise ModelUnavailableError(
                f"Local Ollama inference failed [{InferenceFailureCategory.UNKNOWN_INFERENCE_FAILURE.value}]",
                category=InferenceFailureCategory.UNKNOWN_INFERENCE_FAILURE,
                attempts=attempts,
            )

        prompt_ns = data.get("prompt_eval_duration")
        predicted_ns = data.get("eval_duration")
        return CompletionResult(
            text=data.get("response", ""),
            prompt_tokens=data.get("prompt_eval_count"),
            predicted_tokens=data.get("eval_count"),
            prompt_ms=(prompt_ns / 1_000_000) if prompt_ns is not None else None,
            predicted_ms=(predicted_ns / 1_000_000) if predicted_ns is not None else None,
            total_latency_ms=attempts[-1].get("total_request_duration_ms") or 0.0,
            raw=data,
            request_id=request_id,
            attempt_count=len(attempts),
            inference_attempts=attempts,
        )

    async def _post_generate(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
        prompt: str,
        prompt_hash: str,
        schema_type: str,
        attempt: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        start = time.monotonic()
        diagnostics: dict[str, Any] = {
            "request_id": request_id,
            "attempt": attempt,
            "model": self.model_name,
            "prompt_chars": len(prompt),
            "prompt_tokens": None,
            "prompt_hash": prompt_hash,
            "schema_type": schema_type,
            "request_start_monotonic": start,
            "connect_ms": None,
            "response_headers_ms": None,
            "total_request_duration_ms": None,
            "prompt_eval_duration_ms": None,
            "eval_duration_ms": None,
            "done_reason": None,
            "http_status": None,
            "exception_class": None,
            "exception_message": None,
            "failure_category": None,
            "retryable": False,
            "active_requests_at_start": OllamaClient.active_requests + 1,
        }
        timeout = httpx.Timeout(
            connect=min(self.request_connect_timeout_s, self.request_timeout_s),
            read=self.request_timeout_s,
            write=min(self.request_write_timeout_s, self.request_timeout_s),
            pool=min(self.request_pool_timeout_s, self.request_timeout_s),
        )
        OllamaClient.active_requests += 1
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                request = client.build_request("POST", f"{self.endpoint}/api/generate", json=payload)
                response_start = time.monotonic()
                resp = await client.send(request, stream=True)
                diagnostics["response_headers_ms"] = (time.monotonic() - response_start) * 1000
                body = await resp.aread()
            diagnostics["http_status"] = resp.status_code
            diagnostics["total_request_duration_ms"] = (time.monotonic() - start) * 1000
            if resp.status_code != 200:
                category = (
                    InferenceFailureCategory.SERVICE_UNAVAILABLE
                    if resp.status_code == 503
                    else InferenceFailureCategory.OLLAMA_HTTP_ERROR
                )
                diagnostics["failure_category"] = category.value
                diagnostics["exception_message"] = body.decode("utf-8", errors="replace")[:500]
                diagnostics["retryable"] = category in RETRYABLE_INFERENCE_CATEGORIES
                raise self._error_from_diagnostics(category, diagnostics)
            try:
                data = resp.json()
            except ValueError as exc:
                category = InferenceFailureCategory.MALFORMED_RESPONSE
                diagnostics["failure_category"] = category.value
                diagnostics["exception_class"] = type(exc).__name__
                diagnostics["exception_message"] = str(exc)[:500]
                raise self._error_from_diagnostics(category, diagnostics) from exc
            prompt_ns = data.get("prompt_eval_duration")
            eval_ns = data.get("eval_duration")
            diagnostics.update({
                "prompt_tokens": data.get("prompt_eval_count"),
                "prompt_eval_duration_ms": (prompt_ns / 1_000_000) if prompt_ns is not None else None,
                "eval_duration_ms": (eval_ns / 1_000_000) if eval_ns is not None else None,
                "done_reason": data.get("done_reason"),
                "active_requests_at_end": max(0, OllamaClient.active_requests - 1),
            })
            return data, diagnostics
        except httpx.ConnectTimeout as exc:
            raise self._classified_request_error(InferenceFailureCategory.CONNECT_TIMEOUT, diagnostics, exc) from exc
        except httpx.ReadTimeout as exc:
            raise self._classified_request_error(InferenceFailureCategory.READ_TIMEOUT, diagnostics, exc) from exc
        except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
            raise self._classified_request_error(InferenceFailureCategory.CONNECTION_RESET, diagnostics, exc) from exc
        except httpx.ConnectError as exc:
            raise self._classified_request_error(InferenceFailureCategory.SERVICE_UNAVAILABLE, diagnostics, exc) from exc
        except httpx.TimeoutException as exc:
            raise self._classified_request_error(InferenceFailureCategory.TOTAL_REQUEST_TIMEOUT, diagnostics, exc) from exc
        except httpx.RequestError as exc:
            raise self._classified_request_error(InferenceFailureCategory.UNKNOWN_INFERENCE_FAILURE, diagnostics, exc) from exc
        finally:
            OllamaClient.active_requests = max(0, OllamaClient.active_requests - 1)

    def _classified_request_error(
        self,
        category: InferenceFailureCategory,
        diagnostics: dict[str, Any],
        exc: Exception,
    ) -> ModelUnavailableError:
        diagnostics["total_request_duration_ms"] = (time.monotonic() - diagnostics["request_start_monotonic"]) * 1000
        diagnostics["failure_category"] = category.value
        diagnostics["exception_class"] = type(exc).__name__
        diagnostics["exception_message"] = str(exc)[:500]
        diagnostics["retryable"] = category in RETRYABLE_INFERENCE_CATEGORIES
        diagnostics["active_requests_at_end"] = max(0, OllamaClient.active_requests - 1)
        return self._error_from_diagnostics(category, diagnostics)

    def _error_from_diagnostics(
        self,
        category: InferenceFailureCategory,
        diagnostics: dict[str, Any],
    ) -> ModelUnavailableError:
        retryable = category in RETRYABLE_INFERENCE_CATEGORIES
        detail = diagnostics.get("exception_message") or diagnostics.get("http_status") or ""
        return ModelUnavailableError(
            f"Local Ollama inference failed [{category.value}]:\n{self.endpoint}\n\n"
            f"request_id: {diagnostics['request_id']}\n"
            f"attempt: {diagnostics['attempt']}\n"
            f"detail: {detail}",
            category=category,
            request_id=diagnostics["request_id"],
            retryable=retryable,
            attempts=[diagnostics],
        )

    async def _health_check_after_failure(self, category: InferenceFailureCategory) -> None:
        if category in {
            InferenceFailureCategory.CONNECT_TIMEOUT,
            InferenceFailureCategory.CONNECTION_RESET,
            InferenceFailureCategory.SERVICE_UNAVAILABLE,
        }:
            await self.health_check()


def create_inference_client(config: Any) -> InferenceClient:
    backend = config.model.backend.lower()
    if backend == "llama_cpp":
        return LlamaClient(active_model_endpoint(config), config.model.temperature, config.model.request_timeout_s)
    if backend == "ollama":
        return OllamaClient(
            active_model_endpoint(config),
            config.model.model_name,
            config.model.temperature,
            config.model.request_timeout_s,
            config.model.context_window,
            getattr(config.model, "request_connect_timeout_s", 10.0),
            getattr(config.model, "request_write_timeout_s", 10.0),
            getattr(config.model, "request_pool_timeout_s", 10.0),
            getattr(config.model, "max_inference_attempts", 2),
            getattr(config.model, "inference_retry_backoff_s", 0.5),
            getattr(config.model, "ollama_keep_alive", "5m"),
        )
    raise ValueError(f"unsupported model backend: {config.model.backend!r}")


def active_model_endpoint(config: Any) -> str:
    explicit = getattr(config.model, "endpoint", "") or ""
    if explicit:
        return explicit
    backend = config.model.backend.lower()
    if backend == "llama_cpp":
        return config.model.llamacpp_endpoint
    if backend == "ollama":
        return config.model.ollama_endpoint
    raise ValueError(f"unsupported model backend: {config.model.backend!r}")


def _model_decision_json_schema() -> dict[str, Any]:
    schema = TypeAdapter(ModelAction).json_schema()
    schema["title"] = "BrowserAction"
    return schema


def _classify_httpx_error(exc: httpx.RequestError) -> InferenceFailureCategory:
    if isinstance(exc, httpx.ConnectTimeout):
        return InferenceFailureCategory.CONNECT_TIMEOUT
    if isinstance(exc, httpx.ReadTimeout):
        return InferenceFailureCategory.READ_TIMEOUT
    if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)):
        return InferenceFailureCategory.CONNECTION_RESET
    if isinstance(exc, httpx.ConnectError):
        return InferenceFailureCategory.SERVICE_UNAVAILABLE
    if isinstance(exc, httpx.TimeoutException):
        return InferenceFailureCategory.TOTAL_REQUEST_TIMEOUT
    return InferenceFailureCategory.UNKNOWN_INFERENCE_FAILURE
