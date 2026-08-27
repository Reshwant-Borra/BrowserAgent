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
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

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


def _apply_browser_overrides(config, args: argparse.Namespace) -> None:
    """Lets `--browser-mode`/`--cdp-endpoint` flags override config/default.yaml without the
    user having to hand-edit YAML (Section 18-19: "a normal user should not need to know
    Playwright internals")."""
    if getattr(args, "browser_mode", None):
        config.browser.mode = args.browser_mode
    if getattr(args, "cdp_endpoint", None):
        config.browser.cdp_endpoint = args.cdp_endpoint


def _find_chrome() -> Optional[str]:
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


async def cmd_browser_start(args: argparse.Namespace) -> None:
    chrome = _find_chrome()
    if chrome is None:
        print(
            "Could not find chrome.exe automatically. Start it manually with:\n"
            f'  chrome.exe --remote-debugging-port={args.port} --user-data-dir="{args.profile_dir}"\n'
            "(bind to 127.0.0.1 only — never expose remote debugging to a LAN/public network)."
        )
        sys.exit(1)
    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [chrome, f"--remote-debugging-port={args.port}", f"--user-data-dir={profile_dir}"],
        creationflags=creationflags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"Started persistent Chromium: {chrome}")
    print(f"  remote debugging: http://127.0.0.1:{args.port} (localhost only)")
    print(f"  profile dir:      {profile_dir}")
    print("Log in to any sites you need in that window - the session persists across restarts,")
    print("since BrowserAgent only attaches to it and never stores your passwords.")
    print(f"\nThen start the UI with:\n  browser-agent ui --browser-mode cdp_attach --cdp-endpoint http://127.0.0.1:{args.port}")


async def cmd_browser_status(args: argparse.Namespace) -> None:
    import httpx

    endpoint = args.cdp_endpoint.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{endpoint}/json/version")
            resp.raise_for_status()
            info = resp.json()
        print(f"Browser: Connected ({info.get('Browser', 'unknown')}) at {endpoint}")
    except Exception:
        print(f"Browser: Not connected — no remote-debugging endpoint reachable at {endpoint}")
        print(f'Start one with: browser-agent browser start --port {args.cdp_endpoint.rsplit(":", 1)[-1] if ":" in args.cdp_endpoint else 9222}')
        sys.exit(1)


async def cmd_ui(args: argparse.Namespace) -> None:
    import uvicorn

    from ui.app import create_app

    config = load_config(args.config)
    _apply_browser_overrides(config, args)
    app = create_app(config)
    url = f"http://{args.host}:{args.port}"
    print(f"BrowserAgent UI running at {url}")
    print("Type a task in plain English in the browser tab. Ctrl+C to stop.")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    await server.serve()


async def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _apply_browser_overrides(config, args)
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
    _apply_browser_overrides(config, args)
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
            required_fields=["course", "title", "due_date", "status", "actionable", "source_url", "evidence"],
            field_definitions={
                "status": "one of upcoming, current_incomplete, completed, closed, past_archived, unknown",
                "actionable": "true only when the assignment still requires action",
            },
        )
    if name == "research":
        return ResultContract(
            name="research",
            description="Classify relevance and extract requested facts with source and evidence.",
            required_fields=["pricing", "education_discount", "public_api_docs", "source_url", "evidence"],
            field_definitions={
                "pricing": "pricing, plan, cost, seat, or monthly price information",
                "education_discount": "education, school, student, teacher, or academic discount information",
                "public_api_docs": "public API, REST API, developer documentation, or API docs availability",
            },
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

    p_ui = sub.add_parser("ui", help="start the local BrowserAgent web console (Section 4-6 of Phase 5B)")
    p_ui.add_argument("--host", default="127.0.0.1", help="bind address (never expose beyond localhost)")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab")
    p_ui.add_argument("--browser-mode", choices=["launch", "cdp_attach"], default=None,
                       help="override config/default.yaml's browser.mode for this run")
    p_ui.add_argument("--cdp-endpoint", default=None,
                       help="override config/default.yaml's browser.cdp_endpoint (only used with cdp_attach)")
    p_ui.set_defaults(func=cmd_ui)

    p_run = sub.add_parser("run", help="start a new task")
    p_run.add_argument("goal", help='natural-language task, e.g. "Find the assignment"')
    p_run.add_argument("--criteria", nargs="*", default=[], help="optional success-criteria strings")
    p_run.add_argument("--max-steps", type=int, default=200)
    p_run.add_argument("--browser-mode", choices=["launch", "cdp_attach"], default=None)
    p_run.add_argument("--cdp-endpoint", default=None)
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser("resume", help="resume an existing task by id")
    p_resume.add_argument("task_id")
    p_resume.add_argument("--max-steps", type=int, default=200)
    p_resume.add_argument("--browser-mode", choices=["launch", "cdp_attach"], default=None)
    p_resume.add_argument("--cdp-endpoint", default=None)
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status", help="show a task's current persisted state")
    p_status.add_argument("task_id")
    p_status.set_defaults(func=cmd_status)

    p_browser = sub.add_parser("browser", help="manage the persistent Chromium instance used by cdp_attach mode")
    browser_sub = p_browser.add_subparsers(dest="browser_command", required=True)

    p_browser_start = browser_sub.add_parser("start", help="launch a dedicated, persistent Chromium with remote debugging enabled")
    p_browser_start.add_argument("--port", type=int, default=9222)
    p_browser_start.add_argument("--profile-dir", default="./runtime/browser_profile",
                                  help="dedicated profile directory (never reuses your everyday Chrome profile)")
    p_browser_start.set_defaults(func=cmd_browser_start)

    p_browser_status = browser_sub.add_parser("status", help="check whether a remote-debugging endpoint is reachable")
    p_browser_status.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    p_browser_status.set_defaults(func=cmd_browser_status)

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
