"""CLI entry point: `browser-agent start|stop|status|run|resume|ui|browser|batch`.

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
import os
import sqlite3
import sys
from pathlib import Path

from agent.config import load_config
from agent.loop import AgentLoop
from batch.models import BatchPolicy, NavigationScope, ResultContract, SessionMode
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from cli import launcher
from inference.llama_client import ModelUnavailableError, active_model_endpoint, create_inference_client
from router.extract import extract_urls


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


async def cmd_browser_start(args: argparse.Namespace) -> None:
    """Kept for debugging/backward compatibility; `browser-agent start` is the normal path.

    This used to print "Started persistent Chromium" as soon as subprocess.Popen returned,
    even if Chrome exited immediately and CDP never bound (verified via `netstat` showing
    nothing on the port). It now goes through the same launcher.ensure_chrome_cdp() used by
    `browser-agent start`, which only reports success after /json/version actually answers.
    """
    endpoint = f"http://127.0.0.1:{args.port}"
    step, _pid = launcher.ensure_chrome_cdp(endpoint, Path(args.profile_dir))
    print(launcher.format_step(step))
    if not step.ok:
        sys.exit(1)
    print(f"  profile dir: {args.profile_dir}")
    print("Log in to any sites you need in that window - the session persists across restarts,")
    print("since BrowserAgent only attaches to it and never stores your passwords.")
    print(f"\nThen start the UI with:\n  browser-agent ui --browser-mode cdp_attach --cdp-endpoint {endpoint}")


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

    # cli's --goal is raw text with no router pass, so pull out an explicit URL the same way
    # router/extract.py would — this is what stops "Open https://example.com" from attaching
    # to whatever stale tab a previous cdp_attach task left open (see PlaywrightBackend's
    # explicit_target_url handling / agent/loop.py's AgentLoop.create_new).
    goal_urls = extract_urls(args.goal)
    loop = AgentLoop.create_new(
        config, args.goal, args.criteria or [],
        explicit_target_url=goal_urls[0] if goal_urls else None,
    )
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
    if getattr(args, "task_id", None) is None:
        await cmd_health_status(args, config)
        return
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


async def cmd_health_status(args: argparse.Namespace, config) -> None:
    """`browser-agent status` with no task id: a non-mutating snapshot of the three services
    `browser-agent start` brings up. Never starts or stops anything."""
    ollama_endpoint = config.model.ollama_endpoint
    tags = launcher.check_ollama(ollama_endpoint)
    if tags is None:
        print("Ollama:       NOT RUNNING")
        print(f"Model:        unknown ({config.model.model_name})")
    else:
        print("Ollama:       RUNNING")
        models = [m.get("name", "") for m in tags.get("models", [])]
        model_name = config.model.model_name
        ready = any(m == model_name or m.split(":")[0] == model_name.split(":")[0] for m in models)
        print(f"Model:        {model_name} {'READY' if ready else 'NOT INSTALLED'}")

    cdp_endpoint = config.browser.cdp_endpoint
    cdp_info = launcher.check_cdp(cdp_endpoint)
    print(f"Chrome/CDP:   {'CONNECTED' if cdp_info is not None else 'NOT CONNECTED'} ({cdp_endpoint})")

    ui_host, ui_port = args.host, args.port
    port_in_use, is_ours = launcher.probe_ui_owner(ui_host, ui_port)
    if port_in_use and is_ours:
        print(f"UI:           RUNNING (http://{ui_host}:{ui_port})")
    elif port_in_use:
        print(f"UI:           PORT OCCUPIED BY OTHER PROCESS (http://{ui_host}:{ui_port})")
    else:
        print("UI:           NOT RUNNING")

    if tags is not None:
        gpu_info = launcher.query_gpu_info(ollama_endpoint)
        print(f"GPU:          {gpu_info or '(no model currently loaded - start a task to see GPU usage)'}")


async def cmd_start(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.cdp_endpoint:
        config.browser.cdp_endpoint = args.cdp_endpoint
    runtime_dir = Path(config.storage.runtime_dir)
    paths = launcher.LauncherPaths(runtime_dir)

    try:
        launcher.acquire_start_lock(paths)
    except launcher.AlreadyRunningError as e:
        print(str(e))
        sys.exit(1)

    import atexit
    atexit.register(launcher.release_start_lock, paths)

    print("BrowserAgent startup\n")
    state = launcher.load_state(paths)
    try:
        ollama_endpoint = config.model.ollama_endpoint
        step, pid = launcher.ensure_ollama(ollama_endpoint, config.model.model_name)
        print(launcher.format_step(step))
        if pid:
            state.ollama_pid = pid
            state.ollama_started_by_us = True
            state.ollama_endpoint = ollama_endpoint
        if step.ok:
            gpu_info = launcher.query_gpu_info(ollama_endpoint)
            if gpu_info:
                print(f"       {gpu_info}")
        launcher.save_state(paths, state)
        if not step.ok:
            sys.exit(1)
        print()

        cdp_endpoint = config.browser.cdp_endpoint
        profile_dir = runtime_dir / "browser_profile"
        step, pid = launcher.ensure_chrome_cdp(cdp_endpoint, profile_dir)
        print(launcher.format_step(step))
        if pid:
            state.chrome_pid = pid
            state.chrome_started_by_us = True
            state.chrome_profile_dir = str(profile_dir)
            state.chrome_cdp_endpoint = cdp_endpoint
        launcher.save_state(paths, state)
        if not step.ok:
            sys.exit(1)
        print()

        ui_host, ui_port = args.host, args.port
        port_in_use, is_ours = launcher.probe_ui_owner(ui_host, ui_port)
        if port_in_use and not is_ours:
            print(launcher.format_step(launcher.StepResult(
                False, "Port already in use",
                f"http://{ui_host}:{ui_port} is occupied by something other than BrowserAgent.\n"
                "Free the port or pass --port to use a different one.",
            )))
            sys.exit(1)

        if port_in_use and is_ours:
            print(launcher.format_step(launcher.StepResult(
                True, "BrowserAgent UI", f"http://{ui_host}:{ui_port} (reused existing)",
            )))
            print("\nBrowserAgent is ready.")
            if not args.no_open:
                import webbrowser
                webbrowser.open(f"http://{ui_host}:{ui_port}")
            return

        config.browser.mode = "cdp_attach"
        state.ui_port = ui_port
        state.ui_pid = os.getpid()
        launcher.save_state(paths, state)

        import uvicorn

        from ui.app import create_app

        app = create_app(config)
        server = uvicorn.Server(uvicorn.Config(app, host=ui_host, port=ui_port, log_level="warning"))
        serve_task = asyncio.create_task(server.serve())
        loop = asyncio.get_event_loop()
        ui_info = await loop.run_in_executor(
            None,
            lambda: launcher.poll_until_healthy(lambda: launcher.check_ui(ui_host, ui_port), timeout_s=15.0),
        )
        if ui_info is None:
            print(launcher.format_step(launcher.StepResult(
                False, "BrowserAgent UI failed to start",
                f"Server process started but http://{ui_host}:{ui_port} never responded.",
            )))
            server.should_exit = True
            await serve_task
            sys.exit(1)

        print(launcher.format_step(launcher.StepResult(True, "BrowserAgent UI", f"http://{ui_host}:{ui_port}")))
        print("\nBrowserAgent is ready.")
        if not args.no_open:
            import webbrowser
            webbrowser.open(f"http://{ui_host}:{ui_port}")
        print("Type tasks into the UI. Ctrl+C stops the UI only -")
        print("the persistent Chrome window and Ollama keep running.")
        await serve_task
    finally:
        launcher.release_start_lock(paths)


async def cmd_stop(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    runtime_dir = Path(config.storage.runtime_dir)
    paths = launcher.LauncherPaths(runtime_dir)
    state = launcher.load_state(paths)

    stopped_any = False
    if state.ui_pid and launcher.process_alive(state.ui_pid):
        launcher.stop_pid(state.ui_pid)
        print(f"Stopped BrowserAgent UI (pid {state.ui_pid})")
        state.ui_pid = None
        stopped_any = True
    else:
        print("BrowserAgent UI: not running (or not started by this launcher)")

    if args.browser:
        if state.chrome_started_by_us and state.chrome_pid and launcher.process_alive(state.chrome_pid):
            launcher.stop_pid(state.chrome_pid)
            print(f"Stopped persistent Chrome (pid {state.chrome_pid})")
            state.chrome_pid = None
            stopped_any = True
        else:
            print("Persistent Chrome: not running, or not started by this launcher (left untouched)")

    print("\nOllama is left running (BrowserAgent never stops it automatically).")
    launcher.save_state(paths, state)
    if not stopped_any:
        print("\nNothing to stop.")


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
        # Deliberately goal-agnostic: this contract backs every router-classified "research"
        # job (see ui/jobs.py's _research_contract), from company/pricing lookups to "find the
        # N best <items>" comparison asks. It used to hardcode a fixed fact checklist
        # (pricing/education_discount/public_api_docs) left over from one benchmark fixture
        # (tests/fixtures/multisite/generate_multisite.py's RESEARCH_VARIANTS) and applied it
        # to every research job regardless of what was actually asked — observed live on a
        # "find the 3 best vacuum cleaners" job, whose subgoal plan ended up asking the model
        # to "check for education discount information" and "check for public API
        # documentation availability", wasted steps on those irrelevant checks, and never had
        # a field to hold a product's identity in the first place. `item_name` gives any
        # extracted fact an entity to attach to — required for comparing multiple candidates,
        # not just classifying one page as relevant/irrelevant.
        return ResultContract(
            name="research",
            description=(
                "Classify relevance and extract requested facts with source and evidence. "
                "When the goal asks for multiple candidates (e.g. \"the N best X\"), identify "
                "every distinct candidate item found on the page, not just one example."
            ),
            required_fields=["item_name", "value", "source_url", "evidence"],
            field_definitions={
                "item_name": "the specific product, entity, or item name a fact is about",
                "value": "the requested fact about that item (price, rating, or other distinguishing attribute)",
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


async def cmd_trace(args: argparse.Namespace) -> None:
    from cli import trace as trace_mod

    config = load_config(args.config)

    if args.task:
        task_trace = trace_mod.build_task_trace(config, args.task, label="task")
        if task_trace is None:
            print(f"No such task: {args.task}")
            sys.exit(1)
        print(trace_mod.render_task_trace(task_trace, verbose=args.verbose))
        return

    job = trace_mod.find_recent_job(config) if args.recent else trace_mod.find_job(config, args.job)
    if job is None:
        target = "any UI job" if args.recent else f"job {args.job}"
        print(f"No such job: {target}")
        sys.exit(1)

    print(trace_mod.render_job_header(job))
    print()

    refs = trace_mod.resolve_child_tasks(config, job)
    if not refs:
        print("(no underlying AgentLoop task found for this job)")
        return

    for ref in refs:
        task_trace = trace_mod.build_task_trace(config, ref.task_id, ref.label)
        if task_trace is None:
            print(f"--- {ref.label} (task {ref.task_id}) ---")
            print(f"Status: {ref.status}"
                  + (f"  Failure: {ref.failure_category}" if ref.failure_category else ""))
            print("(no persisted task.db events found for this task)")
            print()
            continue
        print(trace_mod.render_task_trace(task_trace, verbose=args.verbose))


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

    p_start = sub.add_parser("start", help="one-command startup: Ollama + persistent Chrome/CDP + UI, then open the UI")
    p_start.add_argument("--host", default="127.0.0.1", help="UI bind address (never expose beyond localhost)")
    p_start.add_argument("--port", type=int, default=8765, help="UI port")
    p_start.add_argument("--cdp-endpoint", default=None, help="override config/default.yaml's browser.cdp_endpoint")
    p_start.add_argument("--no-open", action="store_true", help="don't auto-open the UI in a browser tab")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop BrowserAgent-owned service processes (never a broad process-name kill)")
    p_stop.add_argument("--browser", action="store_true", help="also stop the dedicated persistent Chrome instance, if BrowserAgent started it")
    p_stop.set_defaults(func=cmd_stop)

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

    p_status = sub.add_parser(
        "status",
        help="show a task's persisted state, or (with no task id) a non-mutating snapshot of Ollama/Chrome/UI health",
    )
    p_status.add_argument("task_id", nargs="?", default=None)
    p_status.add_argument("--host", default="127.0.0.1", help="UI host to probe when task_id is omitted")
    p_status.add_argument("--port", type=int, default=8765, help="UI port to probe when task_id is omitted")
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

    p_trace = sub.add_parser(
        "trace",
        help="reconstruct a human-readable timeline for a job from persisted data only "
             "(never runs or replays anything)",
    )
    trace_target = p_trace.add_mutually_exclusive_group(required=True)
    trace_target.add_argument("--recent", action="store_true", help="most recently created UI job")
    trace_target.add_argument("--job", help="UI job id (runtime/ui/jobs.db)")
    trace_target.add_argument("--task", help="a single AgentLoop task id (runtime/tasks/<id>)")
    p_trace.add_argument("--verbose", action="store_true", help="include raw event payloads per step")
    p_trace.set_defaults(func=cmd_trace)

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
