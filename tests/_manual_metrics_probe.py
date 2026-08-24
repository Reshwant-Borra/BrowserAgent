"""One-off manual probe (not a pytest test) to capture real metrics.jsonl output from a
scripted run, for citing actual measured numbers in docs/PHASE1_REPORT.md instead of
fabricating them. Safe to delete after the report is written."""
import asyncio
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.config import AppConfig, BrowserConfig, ContextConfig, LoggingConfig, ModelConfig, RecoveryConfig, StorageConfig
from agent.loop import AgentLoop
from tests.integration.fake_llama import ScriptedLlamaClient, decision

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "simple_site"


async def main():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    tmp = Path("./runtime/_metrics_probe")
    config = AppConfig(
        model=ModelConfig(endpoint="scripted://fake"),
        browser=BrowserConfig(headless=True, user_data_dir=str(tmp / "tasks"), action_timeout_ms=5000,
                               interactive_approval=False),
        context=ContextConfig(max_page_chars=3000, max_page_chars_deep_recovery=6000,
                               recent_actions=5, max_visible_text_items=12),
        recovery=RecoveryConfig(max_action_retries=2, identical_action_limit=3,
                                 navigation_cycle_limit=2, verification_retry_limit=2),
        storage=StorageConfig(tasks_dir=str(tmp / "tasks")),
        logging=LoggingConfig(level="INFO", dir=str(tmp / "logs"), redact_secrets=True),
    )

    loop = AgentLoop.create_new(config, "Find the price of the Basic plan", ["Basic plan"])
    loop.llama = ScriptedLlamaClient([
        decision("open_url", params={"url": base + "/index.html"}),
        decision("click", target=1, expected_result={"page_contains": "Products"}),
        decision("finish", params={"result": "Basic plan is $9/month"}),
    ])
    state = await loop.run(max_steps=10)
    print("STATUS:", state.status)

    metrics_path = tmp / "logs" / f"{loop.task_id}.metrics.jsonl"
    print("---- metrics.jsonl ----")
    print(metrics_path.read_text(encoding="utf-8"))

    server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
