"""`python -m agent_v2 "<goal>"` — the V2 product entry point.

Deliberately separate from `cli/main.py` so the legacy entry point keeps working untouched
(V2 spec §33). The two share config, the browser backend and the inference client; they
share no control flow.

    python -m agent_v2 "Find X and tell me Y"           # attach to your running Chrome
    python -m agent_v2 --resume v2-ab12cd34ef           # continue after a takeover/crash
    python -m agent_v2 --memories                       # what it has learned so far
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agent.config import load_config
from browser.playwright_backend import BrowserAttachError
from inference.llama_client import OllamaClient, active_model_endpoint, create_inference_client
from agent_v2.agent import BrowserAgentV2, LoopLimits, build_session
from agent_v2.browser_ops import BrowserSession
from agent_v2.memory import MemoryStore
from agent_v2.prompts import ContextBudget
from agent_v2.state import TaskState, TaskStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_v2", description="BrowserAgent V2")
    parser.add_argument("goal", nargs="?", help="what you want done, in plain language")
    parser.add_argument("--config", default=None)
    parser.add_argument("--resume", default=None, metavar="TASK_ID")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--mode", choices=["cdp_attach", "launch"], default="cdp_attach",
                        help="cdp_attach (default) uses the Chrome you already have open")
    parser.add_argument("--cdp-endpoint", default=None)
    parser.add_argument("--start-url", default=None,
                        help="attach to / open this page first instead of the current tab")
    parser.add_argument("--no-memory", action="store_true", help="run without long-term memory")
    parser.add_argument("--memories", action="store_true", help="list durable memories and exit")
    parser.add_argument("--yes", action="store_true",
                        help="auto-approve consequential actions instead of asking")
    parser.add_argument("--quiet", action="store_true")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config.browser.mode = args.mode
    if args.cdp_endpoint:
        config.browser.cdp_endpoint = args.cdp_endpoint

    memory = None if args.no_memory else MemoryStore(config.v2.memory_db)

    if args.memories:
        _print_memories(memory)
        return 0

    tasks_dir = Path(config.v2.tasks_dir)
    if args.resume:
        state_path = tasks_dir / args.resume / "state.json"
        if not state_path.exists():
            print(f"no such task: {args.resume} (looked in {state_path})", file=sys.stderr)
            return 2
        state = TaskState.load(state_path)
        goal, task_id = state.goal, state.task_id
    else:
        if not args.goal:
            print("give me a goal, or --resume <task_id>", file=sys.stderr)
            return 2
        state, goal, task_id = None, args.goal, None

    client = _client(config)
    if not await client.health_check():
        print(f"the local model is not reachable at {active_model_endpoint(config)}.\n"
              f"Start it first (e.g. `ollama serve`) and make sure "
              f"`{config.model.model_name}` is pulled.", file=sys.stderr)
        return 3

    backend = build_session(config, explicit_target_url=args.start_url)
    try:
        await backend.start()
    except BrowserAttachError as exc:
        print(str(exc), file=sys.stderr)
        return 4

    agent = BrowserAgentV2(
        session=BrowserSession(backend),
        client=client,
        memory=memory,
        task_dir=None,
        budget=ContextBudget(max_total_tokens=config.v2.max_total_tokens, blocks={
            "goal": 120, "memory": config.v2.memory_tokens, "state": config.v2.state_tokens,
            "evidence": config.v2.evidence_tokens, "recent": 260, "tabs": 140,
            "page": config.v2.page_tokens, "hint": 260,
        }),
        limits=LoopLimits(max_steps=args.max_steps or config.v2.max_steps),
        max_output_tokens=config.v2.max_output_tokens,
        approval=_approval(args.yes),
        takeover=_takeover(),
        on_step=None if args.quiet else _print_step,
        memory_top_k=config.v2.memory_top_k,
        evidence_top_k=config.v2.evidence_top_k,
    )

    try:
        if args.start_url and not args.resume:
            await backend.open_url(args.start_url)
        if state is not None:
            agent.task_dir = tasks_dir / task_id
            result = await agent.resume(state, max_steps=args.max_steps)
        else:
            import uuid
            task_id = f"v2-{uuid.uuid4().hex[:10]}"
            agent.task_dir = tasks_dir / task_id
            result = await agent.run(goal, task_id=task_id, max_steps=args.max_steps)
    finally:
        # cdp_attach: disconnects the driver only. The user's Chrome, profile, and every tab
        # they had open are left exactly as they were (V2 spec §2/§6).
        await backend.close()
        if memory is not None:
            memory.close()

    _print_result(result, tasks_dir / result.task_id)
    return 0 if result.status == TaskStatus.DONE.value else 1


def _client(config):
    client = create_inference_client(config)
    if isinstance(client, OllamaClient):
        # One model call per browser action: keep the weights resident between steps.
        client.keep_alive = config.v2.keep_alive
        client.request_timeout_s = config.v2.request_timeout_s
    return client


def _approval(auto_yes: bool):
    async def ask(decision, obs) -> bool:
        if auto_yes:
            return True
        if not sys.stdin.isatty():
            print(f"\n[blocked] consequential action needs approval but this is not a terminal: "
                  f"{decision.action.value} \"{decision.target_name}\"")
            return False
        print(f'\n[approval] about to {decision.action.value} "{decision.target_name}" on {obs.url}')
        print(f"           reason: {decision.reason}")
        answer = await asyncio.to_thread(input, "           allow? [y/N] ")
        return answer.strip().lower() in ("y", "yes")
    return ask


def _takeover():
    async def wait(message: str, obs) -> bool:
        print("\n" + "=" * 72)
        print("HANDING OVER TO YOU")
        print(f"  {message}")
        print(f"  page: {obs.url}")
        print("  Do it in the browser window, then come back here.")
        print("  (BrowserAgent never sees or stores what you type.)")
        print("=" * 72)
        if not sys.stdin.isatty():
            print("Not an interactive terminal — pausing. Resume with:")
            print("  python -m agent_v2 --resume <task_id>")
            return False
        await asyncio.to_thread(input, "Press Enter when you're done (or Ctrl-C to stop): ")
        return True
    return wait


def _print_step(event: dict) -> None:
    mark = "ok " if event["ok"] else "!! "
    target = f' "{event["target"]}"' if event.get("target") else ""
    note = f"  <- {event['note']}" if event.get("note") else ""
    print(f"  {mark}{event['step']:>3}. {event['action']}{target}{note}")


def _print_result(state: TaskState, task_dir: Path) -> None:
    metrics = state.metrics
    print("\n" + "=" * 72)
    print(f"{state.status.upper()}  ({state.step} steps, {metrics.total_s:.1f}s)")
    print("=" * 72)
    if state.status == TaskStatus.WAITING_FOR_USER.value:
        print(state.pause_message)
        print(f"\nResume with:  python -m agent_v2 --resume {state.task_id}")
        return
    print(state.answer or "(no answer produced)")
    if state.facts:
        print("\nFacts collected:")
        for fact in state.facts:
            print(f"  - {fact}")
    print(f"\nllm calls {metrics.llm_calls} ({metrics.llm_ms / 1000:.1f}s) | "
          f"actions {metrics.actions_executed} | observe {metrics.observe_ms / 1000:.1f}s | "
          f"browser {metrics.browser_ms / 1000:.1f}s | memory {metrics.memory_ms:.0f}ms | "
          f"max prompt {metrics.prompt_chars_max} chars | "
          f"invalid {metrics.invalid_decisions} | verify-fails {metrics.verification_failures}")
    print(f"trace: {task_dir}")


def _print_memories(memory) -> None:
    if memory is None:
        print("memory is disabled")
        return
    rows = memory.all_active()
    if not rows:
        print("nothing learned yet")
        return
    for row in rows:
        scope = f"{row.type}/{row.domain}" if row.domain else row.type
        print(f"[{row.id:>3}] {scope:<24} used {row.use_count:>2}x  {row.text}")


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
