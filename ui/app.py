"""Local BrowserAgent console (Section 4-6): a FastAPI app + a single static HTML/JS page,
bound to 127.0.0.1 only. `cli/main.py`'s `browser-agent ui` subcommand serves this.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agent.config import AppConfig
from ui.jobs import JobRunner
from ui.store import UIJobStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


class SubmitRequest(BaseModel):
    prompt: str


class ApproveRequest(BaseModel):
    approved: bool


def create_app(config: AppConfig) -> FastAPI:
    runtime_dir = Path(config.storage.runtime_dir)
    store = UIJobStore(runtime_dir / "ui" / "jobs.db")
    runner = JobRunner(config, store, runtime_dir)

    app = FastAPI(title="BrowserAgent")
    app.state.store = store
    app.state.runner = runner

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/browser/status")
    async def browser_status() -> dict[str, Any]:
        """Section 20: small connection indicator, not a dashboard. Only meaningful in
        cdp_attach mode — launch mode always reports connected since AgentLoop starts its own
        Chromium on demand and there's nothing external to check ahead of time."""
        if config.browser.mode != "cdp_attach":
            return {"mode": config.browser.mode, "connected": True}
        import httpx

        endpoint = config.browser.cdp_endpoint.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{endpoint}/json/version")
                resp.raise_for_status()
            return {"mode": "cdp_attach", "connected": True, "endpoint": endpoint}
        except Exception:
            return {
                "mode": "cdp_attach", "connected": False, "endpoint": endpoint,
                "message": (
                    f"Persistent browser is not running at {endpoint}. "
                    "Start it with: browser-agent browser start"
                ),
            }

    @app.post("/api/jobs")
    async def submit_job(req: SubmitRequest) -> dict[str, str]:
        if not req.prompt or not req.prompt.strip():
            raise HTTPException(400, "prompt is empty")
        job_id = runner.submit(req.prompt)
        return {"job_id": job_id}

    @app.get("/api/jobs")
    async def list_jobs() -> list[dict[str, Any]]:
        return store.list_recent()

    @app.delete("/api/jobs")
    async def clear_jobs() -> dict[str, bool]:
        store.clear_history()
        return {"ok": True}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    @app.get("/api/jobs/{job_id}/stream")
    async def stream_job(job_id: str) -> StreamingResponse:
        return StreamingResponse(_sse_generator(store, job_id), media_type="text/event-stream")

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: str, req: ApproveRequest) -> dict[str, bool]:
        ok = runner.approve(job_id, req.approved)
        if not ok:
            raise HTTPException(409, "no pending approval for this job")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/login-continue")
    async def login_continue(job_id: str) -> dict[str, bool]:
        ok = runner.login_continue(job_id)
        if not ok:
            raise HTTPException(409, "job is not waiting on a login")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/stop")
    async def stop_job(job_id: str) -> dict[str, bool]:
        ok = runner.stop(job_id)
        if not ok:
            raise HTTPException(404, "no such running job")
        return {"ok": True}

    return app


async def _sse_generator(store: UIJobStore, job_id: str):
    """Section 58: hand-rolled polling generator, no extra streaming dependency. Polls the
    same persisted snapshot GET /api/jobs/{id} would return, so a client that reconnects via
    a fresh EventSource after a page refresh sees identical data (Section 59)."""
    last_payload: str | None = None
    terminal = {"completed", "failed", "stopped"}
    for _ in range(60 * 60 * 2):  # ~2h ceiling at 1s/poll, well beyond any realistic job
        job = store.get(job_id)
        if job is None:
            yield "event: error\ndata: {}\n\n"
            return
        payload = json.dumps(job, default=str)
        if payload != last_payload:
            yield f"data: {payload}\n\n"
            last_payload = payload
        if job["status"] in terminal:
            return
        await asyncio.sleep(0.5)
