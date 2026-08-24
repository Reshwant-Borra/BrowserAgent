"""Out-of-process worker for the Phase 3 kill test.

Run as `python _kill_test_worker.py <args_json_path>`. Reads a small JSON job description,
runs an AgentLoop with a ScriptedLlamaClient, and — if `die_after_action_step` is set —
calls `os._exit()` immediately after that many browser actions have executed, bypassing all
Python cleanup (no `finally`, no atexit). This faithfully simulates a hard process crash at
a precise, reproducible point (right after a browser action executed but before its
ACTION_RESULT/VERIFICATION_RESULT events are persisted) — the exact ambiguity window
ARCHITECTURE.md's atomicity design exists to handle, without relying on OS signal timing.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.config import AppConfig, BrowserConfig, ContextConfig, LoggingConfig, ModelConfig, RecoveryConfig, StorageConfig  # noqa: E402
from agent.loop import AgentLoop  # noqa: E402
from tests.integration.fake_llama import ScriptedLlamaClient  # noqa: E402


def build_config(job: dict) -> AppConfig:
    return AppConfig(
        model=ModelConfig(endpoint="scripted://fake"),
        browser=BrowserConfig(headless=True, user_data_dir=job["tasks_dir"], action_timeout_ms=5000,
                               interactive_approval=False),
        context=ContextConfig(max_page_chars=3000, max_page_chars_deep_recovery=6000,
                               recent_actions=5, max_visible_text_items=12),
        recovery=RecoveryConfig(max_action_retries=2, identical_action_limit=3,
                                 navigation_cycle_limit=2, verification_retry_limit=2),
        storage=StorageConfig(tasks_dir=job["tasks_dir"]),
        logging=LoggingConfig(level="INFO", dir=job["logs_dir"], redact_secrets=True),
    )


async def run(job: dict) -> None:
    config = build_config(job)
    if job["mode"] == "start":
        loop = AgentLoop.create_new(config, job["goal"], job.get("criteria", []))
        Path(job["task_id_file"]).write_text(loop.task_id, encoding="utf-8")
    else:
        loop = AgentLoop.resume(config, job["task_id"])

    loop.llama = ScriptedLlamaClient(job["script"])

    die_after = job.get("die_after_action_step")
    if die_after:
        original_execute = loop._execute
        counter = {"n": 0}

        async def wrapped_execute(decision, observation):
            result = await original_execute(decision, observation)
            counter["n"] += 1
            if counter["n"] == die_after:
                sys.stdout.flush()
                os._exit(137)  # simulated hard crash: no cleanup, no finally blocks
            return result

        loop._execute = wrapped_execute

    await loop.run(max_steps=job.get("max_steps", 20))


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(run(job))


if __name__ == "__main__":
    main()
