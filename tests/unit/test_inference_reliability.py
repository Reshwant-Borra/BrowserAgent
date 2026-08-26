from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from inference.llama_client import (
    InferenceFailureCategory,
    ModelUnavailableError,
    OllamaClient,
    _classify_httpx_error,
)


def _ollama_response(text: str = '{"action":"finish","result":"ok"}') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "response": text,
            "prompt_eval_count": 12,
            "eval_count": 4,
            "prompt_eval_duration": 1_000_000,
            "eval_duration": 2_000_000,
            "done_reason": "stop",
        },
    )


def test_timeout_classification():
    request = httpx.Request("POST", "http://ollama.test/api/generate")
    assert _classify_httpx_error(httpx.ConnectTimeout("connect", request=request)) == InferenceFailureCategory.CONNECT_TIMEOUT
    assert _classify_httpx_error(httpx.ReadTimeout("read", request=request)) == InferenceFailureCategory.READ_TIMEOUT
    assert _classify_httpx_error(httpx.PoolTimeout("pool", request=request)) == InferenceFailureCategory.TOTAL_REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_retryable_inference_error_retries_then_succeeds():
    posts = 0
    payloads = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        posts += 1
        payloads.append(json.loads(request.content))
        if posts == 1:
            return httpx.Response(503, text="loading")
        return _ollama_response()

    client = OllamaClient(
        "http://ollama.test",
        "qwen3:8b",
        max_inference_attempts=2,
        retry_backoff_s=0,
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete("prompt", grammar="grammar")

    assert result.attempt_count == 2
    assert posts == 2
    assert result.inference_attempts[0]["failure_category"] == InferenceFailureCategory.SERVICE_UNAVAILABLE.value
    assert result.inference_attempts[1]["failure_category"] is None
    assert all(payload.get("keep_alive") == "5m" for payload in payloads)


@pytest.mark.asyncio
async def test_non_retryable_inference_error_does_not_retry():
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(400, text="bad request")

    client = OllamaClient(
        "http://ollama.test",
        "qwen3:8b",
        max_inference_attempts=3,
        retry_backoff_s=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelUnavailableError) as excinfo:
        await client.complete("prompt", grammar="grammar")

    assert posts == 1
    assert excinfo.value.category == InferenceFailureCategory.OLLAMA_HTTP_ERROR
    assert excinfo.value.retryable is False


@pytest.mark.asyncio
async def test_bounded_retry_stops_after_configured_attempts():
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        posts += 1
        return httpx.Response(503, text="still loading")

    client = OllamaClient(
        "http://ollama.test",
        "qwen3:8b",
        max_inference_attempts=2,
        retry_backoff_s=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelUnavailableError) as excinfo:
        await client.complete("prompt", grammar="grammar")

    assert posts == 2
    assert excinfo.value.category == InferenceFailureCategory.SERVICE_UNAVAILABLE
    assert len(excinfo.value.attempts) == 2


@pytest.mark.asyncio
async def test_client_active_request_count_is_cleaned_up_after_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = OllamaClient(
        "http://ollama.test",
        "qwen3:8b",
        max_inference_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelUnavailableError) as excinfo:
        await client.complete("prompt", grammar="grammar")

    assert excinfo.value.category == InferenceFailureCategory.READ_TIMEOUT
    assert OllamaClient.active_requests == 0


@pytest.mark.asyncio
async def test_structured_response_survives_retry():
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        posts += 1
        if posts == 1:
            raise httpx.ReadError("connection reset", request=request)
        return _ollama_response('{"action":"finish","result":"done"}')

    client = OllamaClient(
        "http://ollama.test",
        "qwen3:8b",
        max_inference_attempts=2,
        retry_backoff_s=0,
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete("prompt", grammar="grammar")

    assert result.text == '{"action":"finish","result":"done"}'
    assert result.inference_attempts[0]["failure_category"] == InferenceFailureCategory.CONNECTION_RESET.value
    assert result.inference_attempts[1]["http_status"] == 200


@pytest.mark.asyncio
async def test_malformed_response_is_not_retryable():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = OllamaClient(
        "http://ollama.test",
        "qwen3:8b",
        max_inference_attempts=2,
        retry_backoff_s=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelUnavailableError) as excinfo:
        await client.complete("prompt", grammar="grammar")

    assert excinfo.value.category == InferenceFailureCategory.MALFORMED_RESPONSE
    assert len(excinfo.value.attempts) == 1


def test_no_parallel_requests_leak_after_repeated_failures():
    async def run() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        client = OllamaClient(
            "http://ollama.test",
            "qwen3:8b",
            max_inference_attempts=1,
            transport=httpx.MockTransport(handler),
        )
        for _ in range(3):
            with pytest.raises(ModelUnavailableError):
                await client.complete("prompt", grammar="grammar")
            assert OllamaClient.active_requests == 0

    asyncio.run(run())
