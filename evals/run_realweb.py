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
    trial: int = 1
    failures: list[str] = field(default_factory=list)
    answer: str = ""
    #: FULL_SUCCESS / PARTIAL_GROUNDED / SAFE_FAILURE / UNSUPPORTED_SUCCESS (V2 hardening §29).
    outcome: str = ""
    unsupported_claims: list[str] = field(default_factory=list)
    source_coverage: str = ""
    evidence_records: int = 0
    evidence_rejected: int = 0
    resources_observed: int = 0
    computations: int = 0
    compute_errors: int = 0
    grounding_challenges: int = 0
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

    wanted_status = expect.get("status", TaskStatus.DONE.value)
    if state.status != wanted_status:
        problems.append(f"status={state.status}, expected {wanted_status}")

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

    # Grounding gates (V2 hardening §28). These are not per-task expectations so much as
    # standing conditions: a task that produces the right words while asserting something no
    # page showed has not passed, whatever its answer_contains says.
    if expect.get("no_unsupported_claims", True) and state.unsupported_claims:
        problems.append("answer carries unsupported claims: "
                        + "; ".join(state.unsupported_claims[:3]))

    min_sources = expect.get("min_visited_sources")
    if min_sources and state.metrics.resources_observed < int(min_sources):
        problems.append(f"observed only {state.metrics.resources_observed} sources, "
                        f"wanted {min_sources}")

    min_evidence = expect.get("min_evidence")
    if min_evidence and state.metrics.evidence_records < int(min_evidence):
        problems.append(f"only {state.metrics.evidence_records} evidence records, "
                        f"wanted {min_evidence}")

    if expect.get("requires_computation") and state.metrics.computations == 0:
        problems.append("did no deterministic computation")

    return problems


async def run_task(task: dict, config, memory: Optional[MemoryStore], out_dir: Path,
                   verbose: bool, trial: int = 1) -> TaskResult:
    result = TaskResult(id=task["id"], capability=task.get("capability", ""), goal=task["goal"],
                        success=False, status="not_started", trial=trial)
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
        task_dir=out_dir / f"{task['id']}-t{trial}",
        budget=ContextBudget(max_total_tokens=config.v2.max_total_tokens, blocks={
            "goal": 120, "memory": config.v2.memory_tokens, "state": config.v2.state_tokens,
            "evidence": config.v2.evidence_tokens, "recent": 260, "tabs": 140,
            "page": config.v2.page_tokens, "hint": 260,
        }),
        limits=LoopLimits(max_steps=int(task.get("max_steps", config.v2.max_steps))),
        max_output_tokens=config.v2.max_output_tokens,
        # Unattended: consequential actions are declined rather than performed, and a task
        # that needs a human is recorded as needing one instead of being faked through.
        approval=_decline,
        takeover=None,
        on_step=_printer(task["id"]) if verbose else None,
        memory_top_k=config.v2.memory_top_k,
        evidence_top_k=config.v2.evidence_top_k,
    )

    try:
        if task.get("start_url"):
            # A blip opening the start page must not zero out the task: the agent is
            # perfectly capable of navigating there itself as its first action.
            try:
                await agent.session._goto(backend.page, task["start_url"])
            except Exception as exc:
                result.error = f"start_url did not load ({type(exc).__name__}); agent started anyway"
        state = await agent.run(task["goal"], task_id=f"{task['id']}-t{trial}")
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.total_s = time.time() - started
        return result
    finally:
        # Unattended, so nobody is going to look at the tabs this task opened. Leaving them
        # is how a long suite ends up with several hundred tabs in one profile and takes the
        # browser down with it; the agent itself still never closes a tab it did not open.
        try:
            await agent.session.close_agent_tabs()
        except Exception:
            pass
        await backend.close()

    metrics = state.metrics
    result.status = state.status
    result.answer = state.answer
    result.outcome = state.outcome
    result.unsupported_claims = list(state.unsupported_claims)
    result.source_coverage = state.source_coverage
    result.evidence_records = metrics.evidence_records
    result.evidence_rejected = metrics.evidence_rejected
    result.resources_observed = metrics.resources_observed
    result.computations = metrics.computations
    result.compute_errors = metrics.compute_errors
    result.grounding_challenges = metrics.grounding_challenges
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
    for trial in range(1, args.trials + 1):
        if args.trials > 1:
            print(f"\n########## trial {trial} of {args.trials}", flush=True)
        for task in tasks:
            print(f"\n=== {task['id']} [{task.get('capability', '')}]\n    {task['goal']}",
                  flush=True)
            result = await run_task(task, config, memory, out_dir, verbose=not args.quiet,
                                    trial=trial)
            results.append(result)
            verdict = "PASS" if result.success else "FAIL"
            detail = result.error or ("; ".join(result.failures) or "")
            print(f"    -> {verdict} [{result.outcome or 'n/a'}]  {result.steps} steps, "
                  f"{result.llm_calls} calls, {result.total_s}s, "
                  f"{result.resources_observed} sources, {result.evidence_records} evidence, "
                  f"max prompt {result.max_prompt_chars} chars", flush=True)
            if detail:
                print(f"       {detail}", flush=True)
            if result.answer:
                print(f"       answer: {result.answer[:300]}", flush=True)

    if memory is not None:
        memory.close()

    summary = summarise(args.suite, stamp, results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print_summary(summary, out_dir)
    return 0 if summary["passed"] == summary["total"] and not summary["gates"]["violations"] else 1


def summarise(suite: str, stamp: str, results: list[TaskResult]) -> dict[str, Any]:
    """Aggregate across trials. A single run's pass count says very little about a stochastic
    model, so per-task pass rate and spread are reported alongside it (V2 hardening §27)."""
    by_task: dict[str, list[TaskResult]] = {}
    for result in results:
        by_task.setdefault(result.id, []).append(result)

    per_task = []
    for task_id, runs in by_task.items():
        passes = sum(1 for r in runs if r.success)
        per_task.append({
            "id": task_id,
            "trials": len(runs),
            "passed": passes,
            "pass_rate": round(passes / len(runs), 2),
            "flaky": 0 < passes < len(runs),
            "steps": _spread([r.steps for r in runs]),
            "llm_calls": _spread([r.llm_calls for r in runs]),
            "actions": _spread([r.actions for r in runs]),
            "seconds": _spread([r.total_s for r in runs]),
            "max_prompt_chars": _spread([r.max_prompt_chars for r in runs]),
            "outcomes": sorted({r.outcome for r in runs if r.outcome}),
            "failures": sorted({f for r in runs for f in r.failures}),
        })

    # A run that stopped to ask for a human is none of the four spec outcomes — it has not
    # finished at all — so it is counted separately rather than silently dropped, which would
    # make the outcome tallies quietly fail to add up to the number of runs.
    outcomes: dict[str, int] = {}
    for result in results:
        outcomes[result.outcome or "INCOMPLETE"] = outcomes.get(result.outcome or "INCOMPLETE", 0) + 1

    # The release gates that are about correctness rather than task success.
    violations = []
    for result in results:
        if result.outcome == "UNSUPPORTED_SUCCESS":
            violations.append(f"{result.id} t{result.trial}: UNSUPPORTED_SUCCESS")
        if result.compute_errors and not result.computations:
            violations.append(f"{result.id} t{result.trial}: compute error with no computation")

    return {
        "suite": suite,
        "when": stamp,
        "trials": max((len(v) for v in by_task.values()), default=0),
        "passed": sum(1 for r in results if r.success),
        "total": len(results),
        "tasks_fully_green": sum(1 for t in per_task if t["pass_rate"] == 1.0),
        "tasks_flaky": sum(1 for t in per_task if t["flaky"]),
        "task_count": len(per_task),
        "outcomes": outcomes,
        "gates": {
            "unsupported_success": outcomes.get("UNSUPPORTED_SUCCESS", 0),
            "partial_grounded": outcomes.get("PARTIAL_GROUNDED", 0),
            "safe_failure": outcomes.get("SAFE_FAILURE", 0),
            "full_success": outcomes.get("FULL_SUCCESS", 0),
            "violations": violations,
        },
        "medians": {
            "llm_calls": _median([r.llm_calls for r in results]),
            "actions": _median([r.actions for r in results]),
            "seconds": _median([r.total_s for r in results]),
            "prompt_chars": _median([r.max_prompt_chars for r in results]),
            "evidence_records": _median([r.evidence_records for r in results]),
            "resources_observed": _median([r.resources_observed for r in results]),
        },
        "totals": {
            "llm_calls": sum(r.llm_calls for r in results),
            "actions": sum(r.actions for r in results),
            "seconds": round(sum(r.total_s for r in results), 1),
            "invalid_decisions": sum(r.invalid_decisions for r in results),
            "verification_failures": sum(r.verification_failures for r in results),
            "loop_breaks": sum(r.loop_breaks for r in results),
            "human_interventions": sum(r.human_interventions for r in results),
            "memory_hits": sum(r.memory_hits for r in results),
            "evidence_records": sum(r.evidence_records for r in results),
            "evidence_rejected": sum(r.evidence_rejected for r in results),
            "computations": sum(r.computations for r in results),
            "grounding_challenges": sum(r.grounding_challenges for r in results),
            "max_prompt_chars": max((r.max_prompt_chars for r in results), default=0),
        },
        "per_task": per_task,
        "runs": [asdict(r) for r in results],
    }


def _print_summary(summary: dict, out_dir: Path) -> None:
    print(f"\n{'=' * 78}")
    print(f"{summary['suite']}: {summary['passed']}/{summary['total']} runs passed "
          f"({summary['trials']} trial(s) x {summary['task_count']} tasks)")
    print(f"  fully green tasks: {summary['tasks_fully_green']}/{summary['task_count']}"
          f"   flaky: {summary['tasks_flaky']}")
    print(f"  outcomes: {summary['outcomes']}")
    print(f"  medians:  {summary['medians']}")
    gates = summary["gates"]
    print(f"  UNSUPPORTED_SUCCESS: {gates['unsupported_success']} "
          f"(release gate requires 0)")
    for violation in gates["violations"]:
        print(f"    !! {violation}")
    for task in summary["per_task"]:
        if task["pass_rate"] < 1.0:
            print(f"  {task['id']}: {task['passed']}/{task['trials']} — "
                  + "; ".join(task["failures"][:2]))
    print(f"  {out_dir / 'summary.json'}")


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(float(ordered[middle]), 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _spread(values: list[float]) -> dict[str, float]:
    return {"median": _median(values), "min": round(min(values), 2) if values else 0,
            "max": round(max(values), 2) if values else 0}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_realweb")
    parser.add_argument("--suite", default="dev")
    parser.add_argument("--tasks", default=None, help="comma-separated task ids")
    parser.add_argument("--config", default=None)
    parser.add_argument("--mode", choices=["cdp_attach", "launch"], default="cdp_attach")
    parser.add_argument("--cdp-endpoint", default=None)
    parser.add_argument("--trials", type=int, default=1,
                        help="repeat the whole suite N times; one run is not a measurement")
    parser.add_argument("--memory-db", default=None)
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
