from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AppConfig, BrowserConfig, ContextConfig, LoggingConfig, ModelConfig, RecoveryConfig, StorageConfig  # noqa: E402
from agent.loop import AgentLoop  # noqa: E402
from inference.llama_client import CompletionResult  # noqa: E402


def _decision(action: str, target: Optional[int] = None, params: Optional[dict] = None,
              expected_result: Optional[dict] = None) -> str:
    return json.dumps({
        "action": action,
        "target": target,
        "params": params or {},
        "expected_result": expected_result or {},
        "confidence": 0.9,
    })


class DynamicCrashClient:
    def __init__(self, job: dict):
        self.job = job
        self.endpoint = "scripted://phase4b-crash"
        self.final_stage = 0

    async def health_check(self) -> bool:
        return True

    async def complete(self, prompt: str, grammar: Optional[str] = None, max_tokens: int = 256) -> CompletionResult:
        text = self._choose(prompt)
        return CompletionResult(
            text=text,
            prompt_tokens=len(prompt) // 4,
            predicted_tokens=len(text) // 4,
            total_latency_ms=1.0,
        )

    def _choose(self, prompt: str) -> str:
        if "url: about:blank" in prompt:
            return _decision(
                "open_url",
                params={"url": self.job["start_url"]},
                expected_result={"page_contains": self.job["open_expected"]},
            )
        workflow = self.job["workflow"]
        if workflow == "config":
            return self._config(prompt)
        if workflow == "research":
            return self._research(prompt)
        if workflow == "download":
            return self._download(prompt)
        raise AssertionError(f"unknown workflow: {workflow}")

    def _config(self, prompt: str) -> str:
        if "Configuration saved:" in prompt:
            return _decision("finish", params={"result": "Configuration saved"})
        if "Final configuration" not in prompt:
            return _decision("click", target=1)
        mode, region, token = self.job["values"]
        actions = [
            _decision("select", target=1, params={"value": mode}),
            _decision("select", target=2, params={"value": region}),
            _decision("type", target=3, params={"text": token}),
            _decision("click", target=4, expected_result={"page_contains": "Configuration saved"}),
        ]
        choice = actions[min(self.final_stage, len(actions) - 1)]
        self.final_stage += 1
        return choice

    def _research(self, prompt: str) -> str:
        if "Combined result:" in prompt:
            return _decision("finish", params={"result": "Combined result confirmed"})
        if "Final synthesis" not in prompt:
            return _decision("click", target=1)
        combined = " ".join(self.job["values"])
        if self.final_stage == 0:
            self.final_stage += 1
            return _decision("type", target=1, params={"text": combined})
        self.final_stage += 1
        return _decision("click", target=2, expected_result={"page_contains": f"Combined result: {combined}"})

    def _download(self, prompt: str) -> str:
        if "Download center" not in prompt:
            return _decision("click", target=1)
        return _decision("download", target=self.job["download_target"])


def build_config(job: dict) -> AppConfig:
    return AppConfig(
        model=ModelConfig(endpoint="scripted://phase4b-crash"),
        browser=BrowserConfig(
            headless=True,
            user_data_dir=job["tasks_dir"],
            action_timeout_ms=5000,
            interactive_approval=False,
        ),
        context=ContextConfig(
            max_page_chars=3000,
            max_page_chars_deep_recovery=6000,
            recent_actions=5,
            max_visible_text_items=12,
            enable_running_summary=True,
            enable_memory_retrieval=True,
            enable_active_facts=True,
            enforce_active_fact_constraints=True,
        ),
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

    loop.llama = DynamicCrashClient(job)
    die_after = job.get("die_after_action_step")
    if die_after:
        original_execute = loop._execute
        counter = {"n": 0}

        async def wrapped_execute(decision, observation):
            result = await original_execute(decision, observation)
            counter["n"] += 1
            if counter["n"] == die_after:
                sys.stdout.flush()
                os._exit(137)
            return result

        loop._execute = wrapped_execute

    await loop.run(max_steps=job.get("max_steps", 200))


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(run(job))


if __name__ == "__main__":
    main()
