"""CLI entry point: `browser-agent run|resume|status`.

Kept intentionally thin — argparse only, no framework — since every real behavior lives in
agent.loop.AgentLoop. This module's job is turning operational failures (model server down,
browser launch failure, locked task DB, corrupt resume state) into a short clean message
instead of a raw traceback, per the "no giant uncontrolled traceback for normal user
mistakes" requirement; pass --debug to see the full traceback.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from agent.config import load_config
from agent.loop import AgentLoop
from batch.models import BatchPolicy, NavigationScope, ResultContract, SessionMode
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from inference.llama_client import ModelUnavailableError, active_model_endpoint, create_inference_client


def _print_status(state, task=None) -> None:
    print(f"task_id: {state.task_id}")
    if task is not None:
        print(f"goal: {task.goal}")
    print(f"status: {state.status}")
    print(f"recovery_level: {state.recovery_level}")
    print(f"current_step: {state.current_step}")
    if state.blocked_reason:
        print(f"blocked_reason: {state.blocked_reason}")


async def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    llama = create_inference_client(config)
    if not await llama.health_check():
        print(f"Local model endpoint unavailable:\n{active_model_endpoint(config)}\n\n"
              f"Start the configured {config.model.backend} backend before running the agent.")
        sys.exit(1)

    loop = AgentLoop.create_new(config, args.goal, args.criteria or [])
    print(f"Starting task {loop.task_id}: {args.goal}")
    state = await loop.run(max_steps=args.max_steps)
    _print_status(state)


async def cmd_resume(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    loop = AgentLoop.resume(config, args.task_id)
    print(f"Resuming task {args.task_id}")
    state = await loop.run(max_steps=args.max_steps)
    _print_status(state)


async def cmd_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    from memory.event_store import EventStore
    from memory.task_state import TaskStateStore

    db_path = Path(config.storage.tasks_dir) / args.task_id / "task.db"
    if not db_path.exists():
        print(f"No such task: {args.task_id}")
        sys.exit(1)
    es = EventStore(db_path)
    try:
        store = TaskStateStore(es)
        state = store.load(args.task_id)
        task = store.get_task_record(args.task_id)
        _print_status(state, task)
    finally:
        es.close()


def _batch_dir(config, batch_id: str) -> Path:
    return Path(config.storage.runtime_dir) / "batches" / batch_id


def _load_targets(path: str) -> list[str]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(item) for item in data]
        if isinstance(data, dict) and isinstance(data.get("targets"), list):
            return [str(item) for item in data["targets"]]
        raise ValueError("JSON targets file must be a list or an object with a targets list")
    return [
        line.strip() for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _contract_from_name(name: str) -> ResultContract:
    if name == "assignment":
        return ResultContract(
            name="assignment",
            description="Find actionable assignments only; preserve course, assignment, due date, status, source, and evidence.",
            required_fields=["course", "assignment", "due_date", "status", "source", "evidence"],
        )
    if name == "research":
        return ResultContract(
            name="research",
            description="Classify relevance and extract concise facts with source and evidence.",
            required_fields=["relevant", "fact", "source", "evidence"],
        )
    return ResultContract(name=name)


def _policy_from_args(args: argparse.Namespace) -> BatchPolicy:
    return BatchPolicy(
        continue_on_failure=not args.stop_on_failure,
        work_item_max_attempts=args.max_attempts,
        max_steps_per_item=args.max_steps_per_item,
        max_seconds_per_item=args.max_seconds_per_item,
        max_total_items=args.max_total_items,
        read_only=not args.allow_writes,
        navigation_scope=NavigationScope(args.navigation_scope),
        session_mode=SessionMode(args.session_mode),
    )


async def cmd_batch_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    llama = create_inference_client(config)
    if not await llama.health_check():
        print(f"Local model endpoint unavailable:\n{active_model_endpoint(config)}")
        sys.exit(1)
    batch_id = args.batch_id
    if batch_id is None:
        import uuid
        batch_id = uuid.uuid4().hex[:12]
    store = BatchStore.for_batch_dir(_batch_dir(config, batch_id))
    try:
        contract = _contract_from_name(args.result_schema)
        policy = _policy_from_args(args)
        store.create_batch(args.goal, _load_targets(args.targets), contract, policy, batch_id=batch_id)
        print(f"Starting batch {batch_id}: {args.goal}")
        final = await BatchOrchestrator(config, store, batch_id, policy, contract).run()
        print(json.dumps(final, indent=2) if args.json else _format_batch_progress(store.progress(batch_id)))
    finally:
        store.close()


async def cmd_batch_resume(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = BatchStore.for_batch_dir(_batch_dir(config, args.batch_id))
    try:
        job = store.get_job(args.batch_id)
        contract = ResultContract(**json.loads(job["result_contract"]))
        policy_data = json.loads(job["policy"])
        policy = BatchPolicy(
            **{
                **policy_data,
                "navigation_scope": NavigationScope(policy_data.get("navigation_scope", "same_origin")),
                "session_mode": SessionMode(policy_data.get("session_mode", "isolated_session")),
            }
        )
        final = await BatchOrchestrator(config, store, args.batch_id, policy, contract).run()
        print(json.dumps(final, indent=2) if args.json else _format_batch_progress(store.progress(args.batch_id)))
    finally:
        store.close()


async def cmd_batch_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = BatchStore.for_batch_dir(_batch_dir(config, args.batch_id))
    try:
        progress = store.progress(args.batch_id)
        print(json.dumps(progress, indent=2) if args.json else _format_batch_progress(progress))
    finally:
        store.close()


async def cmd_batch_export(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = BatchStore.for_batch_dir(_batch_dir(config, args.batch_id))
    try:
        job = store.get_job(args.batch_id)
        contract = ResultContract(**json.loads(job["result_contract"]))
        policy_data = json.loads(job["policy"])
        policy = BatchPolicy(
            **{
                **policy_data,
                "navigation_scope": NavigationScope(policy_data.get("navigation_scope", "same_origin")),
                "session_mode": SessionMode(policy_data.get("session_mode", "isolated_session")),
            }
        )
        BatchOrchestrator(config, store, args.batch_id, policy, contract).export_json(Path(args.output))
        print(f"Exported {args.batch_id} to {args.output}")
    finally:
        store.close()


def _format_batch_progress(progress: dict) -> str:
    current = progress.get("current")
    lines = [
        f"Batch: {progress['id']}",
        f"Status: {progress['status']}",
        "",
        f"Completed: {progress['completed']} / {progress['item_count']}",
        f"Running:   {progress['running']}",
        f"Failed:    {progress['failed']}",
        f"Blocked:   {progress['blocked']}",
        f"Pending:   {progress['pending']}",
        f"Duplicates: {progress['duplicates']}",
    ]
    if current:
        lines.extend(["", "Current:", current["target"]])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="browser-agent")
    parser.add_argument("--config", default=None, help="path to a config YAML (default: config/default.yaml)")
    parser.add_argument("--debug", action="store_true", help="show full tracebacks instead of clean error messages")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start a new task")
    p_run.add_argument("goal", help='natural-language task, e.g. "Find the assignment"')
    p_run.add_argument("--criteria", nargs="*", default=[], help="optional success-criteria strings")
    p_run.add_argument("--max-steps", type=int, default=200)
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser("resume", help="resume an existing task by id")
    p_resume.add_argument("task_id")
    p_resume.add_argument("--max-steps", type=int, default=200)
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status", help="show a task's current persisted state")
    p_status.add_argument("task_id")
    p_status.set_defaults(func=cmd_status)

    p_batch = sub.add_parser("batch", help="run or inspect durable multi-target batches")
    batch_sub = p_batch.add_subparsers(dest="batch_command", required=True)

    p_batch_run = batch_sub.add_parser("run", help="start a new batch")
    p_batch_run.add_argument("--targets", required=True, help=".txt one target per line, or JSON list/object")
    p_batch_run.add_argument("--goal", required=True)
    p_batch_run.add_argument("--result-schema", default="generic", choices=["generic", "assignment", "research"])
    p_batch_run.add_argument("--batch-id", default=None)
    p_batch_run.add_argument("--max-attempts", type=int, default=2)
    p_batch_run.add_argument("--max-steps-per-item", type=int, default=20)
    p_batch_run.add_argument("--max-seconds-per-item", type=float, default=120.0)
    p_batch_run.add_argument("--max-total-items", type=int, default=None)
    p_batch_run.add_argument("--navigation-scope", choices=[s.value for s in NavigationScope], default=NavigationScope.SAME_ORIGIN.value)
    p_batch_run.add_argument("--session-mode", choices=[s.value for s in SessionMode], default=SessionMode.ISOLATED.value)
    p_batch_run.add_argument("--allow-writes", action="store_true")
    p_batch_run.add_argument("--stop-on-failure", action="store_true")
    p_batch_run.add_argument("--json", action="store_true")
    p_batch_run.set_defaults(func=cmd_batch_run)

    p_batch_resume = batch_sub.add_parser("resume", help="resume a batch by id")
    p_batch_resume.add_argument("batch_id")
    p_batch_resume.add_argument("--json", action="store_true")
    p_batch_resume.set_defaults(func=cmd_batch_resume)

    p_batch_status = batch_sub.add_parser("status", help="show batch progress")
    p_batch_status.add_argument("batch_id")
    p_batch_status.add_argument("--json", action="store_true")
    p_batch_status.set_defaults(func=cmd_batch_status)

    p_batch_export = batch_sub.add_parser("export", help="export batch results as JSON")
    p_batch_export.add_argument("batch_id")
    p_batch_export.add_argument("--output", required=True)
    p_batch_export.set_defaults(func=cmd_batch_export)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.func(args))
    except ModelUnavailableError as e:
        print(str(e))
        sys.exit(1)
    except sqlite3.OperationalError as e:
        print(f"Task database is locked or unavailable: {e}\n"
              f"(is another browser-agent process already running this task?)")
        if args.debug:
            raise
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        if args.debug:
            raise
        sys.exit(1)
    except Exception as e:  # last-resort clean message for unexpected operational failures
        print(f"Error: {e}")
        if args.debug:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()
