"""Real-web evaluation runner.

Deliberately one-directional: this module imports `agent_v2`, and nothing in `agent_v2`
imports anything here (V2 spec §35). The tasks themselves are YAML data, so adding a task
never involves touching agent code, and no agent code can special-case a task.

    python -m evals.run_realweb --suite dev
    python -m evals.run_realweb --suite holdout --tasks multi-tab,search-and-extract

Results land in runtime/v2/evals/<suite>-<timestamp>.json with per-task metrics.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from agent.config import load_config
from agent_v2.agent import BrowserAgentV2, LoopLimits, build_session
from agent_v2.browser_ops import BrowserSession
from agent_v2.memory import MemoryStore
from agent_v2.prompts import ContextBudget
from agent_v2.state import TaskState, TaskStatus
from inference.llama_client import OllamaClient, create_inference_client

SUITES_DIR = Path(__file__).resolve().parent / "suites"


@dataclass
class TaskResult:
    id: str
    capability: str
    goal: str
    success: bool
    status: str
    failures: list[str] = field(default_factory=list)
    answer: str = ""
    steps: int = 0
    llm_calls: int = 0
    llm_s: float = 0.0
    total_s: float = 0.0
    actions: int = 0
    invalid_decisions: int = 0
    verification_failures: int = 0
    loop_breaks: int = 0
    human_interventions: int = 0
    memory_hits: int = 0
    procedure_hits: int = 0
    max_prompt_chars: int = 0
    observe_s: float = 0.0
    browser_s: float = 0.0
    error: str = ""


def check(expect: dict[str, Any], state: TaskState) -> list[str]:
    """Objective validation. Returns the list of checks that failed — an empty list is a
    pass. "The agent produced an answer" is never on its own sufficient (V2 spec §35)."""
    answer = state.answer or ""
    lowered = answer.lower()
    problems: list[str] = []

    if state.status != TaskStatus.DONE.value:
        problems.append(f"status={state.status}")

    for needle in expect.get("answer_contains_all", []):
        if needle.lower() not in lowered:
            problems.append(f"answer is missing {needle!r}")

    any_of = expect.get("answer_contains_any")
    if any_of and not any(n.lower() in lowered for n in any_of):
        problems.append(f"answer contains none of {any_of}")

    pattern = expect.get("answer_matches")
    if pattern and not re.search(pattern, answer, re.I):
        problems.append(f"answer does not match /{pattern}/")

    # A positive pattern alone is easy to satisfy accidentally — "Python is higher than
    # Node.js" contains both "higher" and "Node". Where a task has a wrong answer that is
    # as fluent as the right one, the wrong one gets named explicitly.
    forbidden = expect.get("answer_not_matches")
    if forbidden and re.search(forbidden, answer, re.I):
        problems.append(f"answer matches the known-wrong pattern /{forbidden}/")

    min_chars = expect.get("min_answer_chars")
    if min_chars and len(answer.strip()) < int(min_chars):
        problems.append(f"answer is only {len(answer.strip())} chars, wanted {min_chars}")

    min_facts = expect.get("min_facts")
    if min_facts and len(state.facts) + state.spilled_facts < int(min_facts):
        problems.append(f"only {len(state.facts)} facts collected, wanted {min_facts}")

    domain = expect.get("final_domain")
    if domain and domain not in state.current_url:
        problems.append(f"ended on {state.current_url}, expected {domain}")

    # The check that catches a confident answer recited from the model's own weights rather
    # than read off a page: a multi-source task that never loaded the second source did not
    # do the task, however plausible the sentence it produced.
    for required in expect.get("visited_domains", []):
        if not any(required in visited for visited in state.domains):
            problems.append(f"never visited {required} (visited: {', '.join(state.domains) or 'nothing'})")

    return problems


async def run_task(task: dict, config, memory: Optional[MemoryStore], out_dir: Path,
                   verbose: bool) -> TaskResult:
    result = TaskResult(id=task["id"], capability=task.get("capability", ""), goal=task["goal"],
                        success=False, status="not_started")
    backend = build_session(config, explicit_target_url=task.get("start_url"))
    started = time.time()
    try:
        await backend.start()
    except Exception as exc:
        result.error = f"browser attach failed: {exc}"
        return result

    client = create_inference_client(config)
    if isinstance(client, OllamaClient):
        client.keep_alive = config.v2.keep_alive
        client.request_timeout_s = config.v2.request_timeout_s

    agent = BrowserAgentV2(
        session=BrowserSession(backend),
        client=client,
        memory=memory,
        task_dir=out_dir / task["id"],
        budget=ContextBudget(max_total_tokens=config.v2.max_total_tokens, blocks={
            "goal": 120, "memory": config.v2.memory_tokens, "state": config.v2.state_tokens,
            "recent": 260, "tabs": 140, "page": config.v2.page_tokens, "hint": 260,
        }),
        limits=LoopLimits(max_steps=int(task.get("max_steps", config.v2.max_steps))),
        max_output_tokens=config.v2.max_output_tokens,
        # Unattended: consequential actions are declined rather than performed, and a task
        # that needs a human is recorded as needing one instead of being faked through.
        approval=_decline,
        takeover=None,
        on_step=_printer(task["id"]) if verbose else None,
        memory_top_k=config.v2.memory_top_k,
    )

    try:
        if task.get("start_url"):
            await backend.open_url(task["start_url"])
        state = await agent.run(task["goal"], task_id=task["id"])
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.total_s = time.time() - started
        return result
    finally:
        await backend.close()

    metrics = state.metrics
    result.status = state.status
    result.answer = state.answer
    result.steps = state.step
    result.llm_calls = metrics.llm_calls
    result.llm_s = round(metrics.llm_ms / 1000, 2)
    result.total_s = round(metrics.total_s, 2)
    result.actions = metrics.actions_executed
    result.invalid_decisions = metrics.invalid_decisions
    result.verification_failures = metrics.verification_failures
    result.loop_breaks = metrics.loop_breaks
    result.human_interventions = metrics.human_interventions
    result.memory_hits = metrics.memory_hits
    result.procedure_hits = metrics.procedure_hits
    result.max_prompt_chars = metrics.prompt_chars_max
    result.observe_s = round(metrics.observe_ms / 1000, 2)
    result.browser_s = round(metrics.browser_ms / 1000, 2)
    result.failures = check(task.get("expect", {}) or {}, state)
    result.success = not result.failures
    return result


async def _decline(decision, obs) -> bool:
    return False


def _printer(task_id: str):
    def emit(event: dict) -> None:
        mark = "ok" if event["ok"] else "!!"
        note = f"  <- {event['note']}" if event.get("note") else ""
        print(f"    {mark} {event['step']:>3}. {event['action']} "
              f"{event.get('target') or ''}{note}", flush=True)
    return emit


async def main_async(args) -> int:
    suite_path = SUITES_DIR / f"{args.suite}.yaml"
    if not suite_path.exists():
        print(f"no such suite: {suite_path}", file=sys.stderr)
        return 2
    tasks = yaml.safe_load(suite_path.read_text(encoding="utf-8"))["tasks"]
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in tasks if t["id"] in wanted]
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 2

    config = load_config(args.config)
    config.browser.mode = args.mode
    if args.cdp_endpoint:
        config.browser.cdp_endpoint = args.cdp_endpoint

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(config.storage.runtime_dir) / "v2" / "evals" / f"{args.suite}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    memory = None if args.no_memory else MemoryStore(args.memory_db or config.v2.memory_db)

    results: list[TaskResult] = []
    for task in tasks:
        print(f"\n=== {task['id']} [{task.get('capability', '')}]\n    {task['goal']}", flush=True)
        result = await run_task(task, config, memory, out_dir, verbose=not args.quiet)
        results.append(result)
        verdict = "PASS" if result.success else "FAIL"
        detail = result.error or ("; ".join(result.failures) or "")
        print(f"    -> {verdict}  {result.steps} steps, {result.llm_calls} calls, "
              f"{result.total_s}s, max prompt {result.max_prompt_chars} chars", flush=True)
        if detail:
            print(f"       {detail}", flush=True)
        if result.answer:
            print(f"       answer: {result.answer[:300]}", flush=True)

    if memory is not None:
        memory.close()

    passed = sum(1 for r in results if r.success)
    summary = {
        "suite": args.suite,
        "when": stamp,
        "passed": passed,
        "total": len(results),
        "totals": {
            "llm_calls": sum(r.llm_calls for r in results),
            "actions": sum(r.actions for r in results),
            "seconds": round(sum(r.total_s for r in results), 1),
            "invalid_decisions": sum(r.invalid_decisions for r in results),
            "verification_failures": sum(r.verification_failures for r in results),
            "loop_breaks": sum(r.loop_breaks for r in results),
            "human_interventions": sum(r.human_interventions for r in results),
            "memory_hits": sum(r.memory_hits for r in results),
            "max_prompt_chars": max((r.max_prompt_chars for r in results), default=0),
        },
        "tasks": [asdict(r) for r in results],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(results)} passed — {out_dir / 'summary.json'}")
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_realweb")
    parser.add_argument("--suite", default="dev")
    parser.add_argument("--tasks", default=None, help="comma-separated task ids")
    parser.add_argument("--config", default=None)
    parser.add_argument("--mode", choices=["cdp_attach", "launch"], default="cdp_attach")
    parser.add_argument("--cdp-endpoint", default=None)
    parser.add_argument("--memory-db", default=None)
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
