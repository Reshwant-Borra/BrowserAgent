"""Demonstrates durable memory across two unrelated tasks, on live pages.

    python -m evals.demo_memory

Task 1 runs and writes what it learned. Task 2 is a *different* goal on the same site and
must receive the useful memory and none of the irrelevant ones. The point being shown is
selectivity: the store also contains decoy memories from other domains, and those must not
reach the prompt (V2 spec §14 and deliverable H).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.config import load_config
from agent_v2.agent import BrowserAgentV2, LoopLimits, build_session
from agent_v2.browser_ops import BrowserSession
from agent_v2.memory import MemoryStore
from agent_v2.state import TaskState
from inference.llama_client import OllamaClient, create_inference_client

SITE = "https://quotes.toscrape.com/"
DECOYS = [
    ("site", "The archive page needs a date filter before it shows any results", "archive.example"),
    ("lesson", "Downloading the quarterly spreadsheet needs two separate confirmations", "finance.example"),
    ("preference", "The user prefers nonstop flights over connections", ""),
    ("site", "Search results only appear after pressing Enter, the button is decorative", "shop.example"),
]


async def run_task(config, memory: MemoryStore, goal: str, start_url: str,
                   task_id: str, out: Path) -> tuple[TaskState, list[str]]:
    backend = build_session(config, explicit_target_url=start_url)
    await backend.start()
    client = create_inference_client(config)
    if isinstance(client, OllamaClient):
        client.keep_alive = config.v2.keep_alive
        client.request_timeout_s = config.v2.request_timeout_s
    agent = BrowserAgentV2(session=BrowserSession(backend), client=client, memory=memory,
                           task_dir=out / task_id, limits=LoopLimits(max_steps=12))
    injected: list[str] = []
    original = agent._retrieve

    def spy(state, obs):                      # capture exactly what memory reached the prompt
        rendered = original(state, obs)
        if rendered:
            injected.append(rendered)
        return rendered

    agent._retrieve = spy
    try:
        await agent.session._goto(backend.page, start_url)
        state = await agent.run(goal, task_id=task_id)
    finally:
        await backend.close()
    return state, injected


async def main() -> int:
    config = load_config()
    config.browser.mode = "cdp_attach"
    out = Path(config.storage.runtime_dir) / "v2" / "demos" / "memory"
    db = out / "memory.sqlite3"
    if db.exists():
        db.unlink()
    out.mkdir(parents=True, exist_ok=True)
    memory = MemoryStore(db)
    for kind, text, domain in DECOYS:
        memory.save(kind, text, domain=domain, importance=0.6)

    print("=" * 78)
    print("TASK 1 — learn something")
    print("=" * 78)
    state1, _ = await run_task(config, memory,
                               "List the authors of the first three quotes on this page.",
                               SITE, "demo-memory-1", out)
    print(f"  status={state1.status}  steps={state1.step}")
    print(f"  answer: {state1.answer[:200]}")

    learned = [m for m in memory.all_active()
               if m.text not in {d[1] for d in DECOYS}]
    print("\n  memories written by task 1:")
    for m in learned:
        print(f"    [{m.type}/{m.domain or 'any'}] {m.text}")
    if not learned:
        print("    (none judged durable — a valid outcome, but this demo needs one to continue)")

    print("\n" + "=" * 78)
    print("TASK 2 — different goal, same site: what gets retrieved?")
    print("=" * 78)
    state2, injected = await run_task(config, memory,
                                      "How many tags are listed in the Top Ten tags box on this page?",
                                      SITE, "demo-memory-2", out)
    print(f"  status={state2.status}  steps={state2.step}  memory hits={state2.metrics.memory_hits}")
    print(f"  answer: {state2.answer[:200]}")

    seen = "\n".join(injected)
    print("\n  memory actually injected into task 2's prompts:")
    for line in sorted({l.strip() for l in seen.splitlines() if l.strip()}):
        print(f"    {line}")

    print("\n  decoys (must NOT appear above):")
    leaked = [text for _k, text, _d in DECOYS if text.lower()[:40] in seen.lower()]
    for _kind, text, domain in DECOYS:
        mark = "LEAKED" if text in leaked else "not injected"
        print(f"    [{mark}] ({domain or 'any'}) {text[:60]}")

    total = len(memory.all_active())
    memory.close()
    print(f"\n  store holds {total} active memories; task 2 was shown "
          f"{state2.metrics.memory_hits}.")
    print("  RESULT:", "FAIL — an irrelevant memory reached the prompt" if leaked
          else "PASS — only relevant memory was injected")
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
