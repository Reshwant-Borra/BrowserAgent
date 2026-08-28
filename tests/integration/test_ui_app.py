"""FastAPI HTTP-layer tests (Section 29/76) on top of `tests/integration/test_ui_jobs.py`'s
JobRunner coverage: confirms the routes themselves are wired correctly (submit, status,
history, approve, stop, login-continue, 404/409 handling). Uses the same deterministic
FakeUIClient pattern so no live model/Ollama is needed.

Uses `httpx.AsyncClient` against the ASGI app directly (not Starlette's synchronous
`TestClient`) for anything that waits on the JobRunner's background `asyncio.create_task`:
TestClient's synchronous wrapper only pumps its portal event loop while actively handling one
request, so a background job has no opportunity to make progress between polling calls made
with a blocking `time.sleep`. A real `AsyncClient` shares one continuously-running event loop
with the app, so `await asyncio.sleep(...)` between polls actually lets the job advance —
matching how a real browser tab polling a real uvicorn server behaves.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.config import AppConfig
from inference.llama_client import CompletionResult
from router.extract import extract_urls
from ui.app import create_app


class FakeUIClient:
    endpoint = "fake://ui"

    def __init__(self):
        self.calls = 0

    async def health_check(self) -> bool:
        return True

    async def complete(self, prompt, grammar=None, max_tokens=256, json_schema=None) -> CompletionResult:
        self.calls += 1
        if grammar is None:
            raise AssertionError("router model fallback should not be needed for these prompts")
        if "about:blank" in prompt:
            urls = extract_urls(prompt)
            url = urls[-1] if urls else "http://127.0.0.1"
            action = {"action": "open_url", "target": None, "params": {"url": url},
                      "expected_result": {"url_contains": "127.0.0.1"}, "confidence": 0.9}
        else:
            action = {"action": "finish", "target": None, "params": {"result": "fixture page summary"},
                      "expected_result": {}, "confidence": 0.9}
        return CompletionResult(text=json.dumps(action), total_latency_ms=1.0)


def _sync_client(tmp_config: AppConfig, monkeypatch) -> TestClient:
    fake = FakeUIClient()
    monkeypatch.setattr("agent.loop.create_inference_client", lambda config: fake)
    monkeypatch.setattr("ui.jobs.create_inference_client", lambda config: fake)
    return TestClient(create_app(tmp_config))


def _async_client(tmp_config: AppConfig, monkeypatch) -> httpx.AsyncClient:
    fake = FakeUIClient()
    monkeypatch.setattr("agent.loop.create_inference_client", lambda config: fake)
    monkeypatch.setattr("ui.jobs.create_inference_client", lambda config: fake)
    app = create_app(tmp_config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _wait_for(client: httpx.AsyncClient, job_id: str, statuses: set[str], timeout: float = 10.0) -> dict:
    elapsed = 0.0
    while elapsed < timeout:
        resp = await client.get(f"/api/jobs/{job_id}")
        job = resp.json()
        if job["status"] in statuses:
            return job
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError(f"job {job_id} did not reach {statuses} within {timeout}s (last: {job})")


def test_index_serves_html(tmp_config, monkeypatch):
    client = _sync_client(tmp_config, monkeypatch)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "BrowserAgent" in resp.text


def test_submit_empty_prompt_rejected(tmp_config, monkeypatch):
    client = _sync_client(tmp_config, monkeypatch)
    resp = client.post("/api/jobs", json={"prompt": "   "})
    assert resp.status_code == 400


def test_get_unknown_job_404s(tmp_config, monkeypatch):
    client = _sync_client(tmp_config, monkeypatch)
    resp = client.get("/api/jobs/does-not-exist")
    assert resp.status_code == 404


def test_approve_without_pending_approval_409s(tmp_config, monkeypatch):
    client = _sync_client(tmp_config, monkeypatch)
    resp = client.post("/api/jobs/nonexistent-job/approve", json={"approved": True})
    assert resp.status_code == 409


def test_stop_unknown_job_404s(tmp_config, monkeypatch):
    client = _sync_client(tmp_config, monkeypatch)
    resp = client.post("/api/jobs/nonexistent-job/stop")
    assert resp.status_code == 404


def test_stop_is_idempotent_over_http_on_terminal_and_orphaned_jobs(tmp_config, monkeypatch):
    """Section 13: stop must never 500, and must return a harmless {"ok": true} both for an
    already-terminal job and for a persisted waiting_for_input job with no live driving
    process behind it (the server-restart case — Section 11)."""
    client = _sync_client(tmp_config, monkeypatch)
    app = client.app
    store = app.state.store

    completed_id = store.create("already done")
    store.update(completed_id, status="completed")
    resp = client.post(f"/api/jobs/{completed_id}/stop")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert store.get(completed_id)["status"] == "completed"

    orphaned_id = store.create("orphaned after restart")
    store.update(orphaned_id, status="waiting_for_input", pending_clarification={"question": "Which pages?"})
    resp = client.post(f"/api/jobs/{orphaned_id}/stop")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    job = store.get(orphaned_id)
    assert job["status"] == "stopped"
    assert job["pending_clarification"] is None

    # idempotent: stopping it again is still a harmless 200, not a 404/500.
    resp = client.post(f"/api/jobs/{orphaned_id}/stop")
    assert resp.status_code == 200


def test_login_continue_without_pending_login_409s(tmp_config, monkeypatch):
    client = _sync_client(tmp_config, monkeypatch)
    resp = client.post("/api/jobs/nonexistent-job/login-continue")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_submit_and_poll_to_completion(tmp_config, monkeypatch, fixture_site_url):
    async with _async_client(tmp_config, monkeypatch) as client:
        resp = await client.post("/api/jobs", json={"prompt": f"Open {fixture_site_url}/index.html and summarize it."})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        job = await _wait_for(client, job_id, {"completed", "failed"})
        assert job["status"] == "completed"
        assert job["kind"] == "single_site"
        assert "fixture page summary" in job["final_result"]["summary"]


@pytest.mark.asyncio
async def test_history_lists_and_clears(tmp_config, monkeypatch, fixture_site_url):
    async with _async_client(tmp_config, monkeypatch) as client:
        resp = await client.post("/api/jobs", json={"prompt": f"Open {fixture_site_url}/index.html and summarize it."})
        job_id = resp.json()["job_id"]
        await _wait_for(client, job_id, {"completed", "failed"})

        history = (await client.get("/api/jobs")).json()
        assert any(j["id"] == job_id for j in history)

        clear_resp = await client.delete("/api/jobs")
        assert clear_resp.status_code == 200
        assert (await client.get("/api/jobs")).json() == []


@pytest.mark.asyncio
async def test_stream_endpoint_returns_sse_snapshot(tmp_config, monkeypatch, fixture_site_url):
    async with _async_client(tmp_config, monkeypatch) as client:
        resp = await client.post("/api/jobs", json={"prompt": f"Open {fixture_site_url}/index.html and summarize it."})
        job_id = resp.json()["job_id"]
        await _wait_for(client, job_id, {"completed", "failed"})

        async with client.stream("GET", f"/api/jobs/{job_id}/stream") as stream_resp:
            assert stream_resp.status_code == 200
            assert stream_resp.headers["content-type"].startswith("text/event-stream")
            async for line in stream_resp.aiter_lines():
                if line:
                    assert line.startswith("data: ")
                    payload = json.loads(line[len("data: "):])
                    assert payload["id"] == job_id
                    break
