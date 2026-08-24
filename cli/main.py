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
import sqlite3
import sys
from pathlib import Path

from agent.config import load_config
from agent.loop import AgentLoop
from inference.llama_client import ModelUnavailableError, create_inference_client


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
        print(f"Local model endpoint unavailable:\n{config.model.endpoint}\n\n"
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
