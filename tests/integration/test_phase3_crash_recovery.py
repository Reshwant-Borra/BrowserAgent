"""Phase 3 exit gate: the kill-test harness. A real subprocess executes a real browser
action, is terminated abruptly (os._exit, no cleanup) at a precise point, and a second,
independent process resumes the same task_id from the same on-disk state and finishes it
without duplicating the consequential action that was mid-flight when it died.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from memory.event_store import EventStore, EventType
from memory.replay import replay_task
from memory.task_state import TaskStateStore
from tests.integration.fake_llama import decision

WORKER = Path(__file__).resolve().parent / "_kill_test_worker.py"


def run_worker(job: dict, job_path: Path, timeout: int = 60) -> int:
    job_path.write_text(json.dumps(job), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(WORKER), str(job_path)], timeout=timeout)
    return proc.returncode


def _reap_orphaned_browser_processes(profile_marker: str) -> None:
    """os._exit() kills only the Python worker, not the Chromium process Playwright
    spawned under it — that process is left running (orphaned) and would otherwise hold
    the persistent-context profile lock, making the resume attempt hang/fail for a reason
    that has nothing to do with the crash-recovery logic actually under test."""
    try:
        import psutil
    except ImportError:
        return
    marker = profile_marker.replace("\\", "/").lower()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or []).replace("\\", "/").lower()
            if marker in cmdline:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def test_kill_mid_action_and_resume(tmp_path, fixture_site_url):
    tasks_dir = tmp_path / "tasks"
    logs_dir = tmp_path / "logs"
    task_id_file = tmp_path / "task_id.txt"
    job_path = tmp_path / "job.json"

    start_job = {
        "mode": "start",
        "tasks_dir": str(tasks_dir),
        "logs_dir": str(logs_dir),
        "goal": "Submit the wizard application",
        "criteria": [],
        "task_id_file": str(task_id_file),
        "die_after_action_step": 2,  # dies right after the click (the 2nd real browser action)
        "max_steps": 10,
        "script": [
            decision("open_url", params={"url": fixture_site_url + "/wizard_confirm.html"}),
            decision("click", target=1, expected_result={"page_contains": "submitted 1 time"}),
            decision("finish", params={"result": "should never be reached before the simulated crash"}),
        ],
    }
    returncode = run_worker(start_job, job_path)
    assert returncode == 137, "worker should have hit the simulated crash, not run to completion"

    task_id = task_id_file.read_text(encoding="utf-8").strip()
    db_path = tasks_dir / task_id / "task.db"
    _reap_orphaned_browser_processes(str(tasks_dir / task_id))

    es = EventStore(db_path)
    store = TaskStateStore(es)
    state_before_resume = store.load(task_id)
    assert state_before_resume.pending_action_intent is not None, \
        "the click's ACTION_INTENT should be on disk with no matching ACTION_RESULT"
    assert state_before_resume.status == "running"
    es.close()

    # The resume process must NOT need any further scripted decisions: reconciliation
    # itself (not a model call) is what resolves this step. An empty script means "assert
    # no llama.complete() call happens here" — if one did, ScriptedLlamaClient would raise.
    resume_job = {
        "mode": "resume",
        "tasks_dir": str(tasks_dir),
        "logs_dir": str(logs_dir),
        "task_id": task_id,
        "max_steps": 10,
        "script": [],
    }
    returncode2 = run_worker(resume_job, job_path)
    assert returncode2 == 0

    es2 = EventStore(db_path)
    store2 = TaskStateStore(es2)
    final_state = store2.load(task_id)
    events = es2.all_events(task_id)
    es2.close()

    # This is a same-page, storage-less client-side JS mutation with no server/URL/cookie
    # record of it — genuinely unrecoverable by re-observation after the browser tab itself
    # is gone (see docs/PHASE3_REPORT.md). The system's job is not to guess; it's to refuse
    # to silently resubmit and instead escalate for human review. That is what must be
    # verified here: NOT that the task blindly completes.
    assert final_state.status == "blocked"
    assert "consequential" in (final_state.blocked_reason or "").lower()
    assert final_state.pending_action_intent is None  # resolved (to "blocked"), not left dangling

    replayed = replay_task(task_id, events)
    assert replayed.status == "blocked"

    action_results_for_click = [
        e for e in events if e.type == EventType.ACTION_RESULT
        and e.payload.get("action_fingerprint") == "click:1:{}"
    ]
    assert len(action_results_for_click) == 1, "the click must never be re-executed after resume"

    verification_events = [
        e for e in events if e.type == EventType.VERIFICATION_RESULT
        and e.payload.get("action_fingerprint") == "click:1:{}"
    ]
    assert len(verification_events) == 1
    assert verification_events[0].verification_result["passed"] is False
    assert verification_events[0].payload.get("note") == "post-resume reconciliation of an interrupted action"


def test_kill_after_readonly_navigation_reconciles_and_completes(tmp_path, fixture_site_url):
    """The counterpart scenario: when the ambiguous action is read-only/idempotent
    (open_url), reconciliation CAN positively confirm it succeeded — proving the resume
    path is not merely conservative-by-default but actually resolves what it safely can."""
    tasks_dir = tmp_path / "tasks"
    logs_dir = tmp_path / "logs"
    task_id_file = tmp_path / "task_id.txt"
    job_path = tmp_path / "job.json"
    products_url = fixture_site_url + "/products.html"

    start_job = {
        "mode": "start",
        "tasks_dir": str(tasks_dir),
        "logs_dir": str(logs_dir),
        "goal": "Read the Basic plan price",
        "criteria": [],
        "task_id_file": str(task_id_file),
        "die_after_action_step": 1,  # dies right after the open_url navigation completes
        "max_steps": 10,
        "script": [
            decision("open_url", params={"url": products_url}, expected_result={"page_contains": "Basic plan"}),
            decision("finish", params={"result": "should never be reached before the simulated crash"}),
        ],
    }
    assert run_worker(start_job, job_path) == 137

    task_id = task_id_file.read_text(encoding="utf-8").strip()
    db_path = tasks_dir / task_id / "task.db"
    _reap_orphaned_browser_processes(str(tasks_dir / task_id))

    es = EventStore(db_path)
    store = TaskStateStore(es)
    state_before_resume = store.load(task_id)
    assert state_before_resume.pending_action_intent is not None
    es.close()

    resume_job = {
        "mode": "resume", "tasks_dir": str(tasks_dir), "logs_dir": str(logs_dir),
        "task_id": task_id, "max_steps": 10,
        "script": [decision("finish", params={"result": "confirmed via reconciliation"})],
    }
    assert run_worker(resume_job, job_path) == 0

    es2 = EventStore(db_path)
    final_state = TaskStateStore(es2).load(task_id)
    events = es2.all_events(task_id)
    es2.close()

    assert final_state.status == "completed"
    assert final_state.pending_action_intent is None

    fingerprint = f"open_url:None:{{'url': '{products_url}'}}"
    verification_events = [e for e in events if e.type == EventType.VERIFICATION_RESULT
                            and e.payload.get("action_fingerprint") == fingerprint]
    assert len(verification_events) == 1
    assert verification_events[0].verification_result["passed"] is True
    assert verification_events[0].payload.get("note") == "post-resume reconciliation of an interrupted action"

    # open_url is idempotent/read-only, so reconciliation re-visiting it once to inspect
    # state is not a forbidden "retry" — but the *original* pending intent must still never
    # be treated as if a NEW decision chose it again.
    action_intents = [e for e in events if e.type == EventType.ACTION_INTENT
                       and e.payload.get("action_fingerprint") == fingerprint]
    assert len(action_intents) == 1


def test_repeated_resume_of_completed_task_is_a_noop(tmp_path, fixture_site_url):
    """Resuming a task that already completed must not corrupt state or repeat actions."""
    tasks_dir = tmp_path / "tasks"
    logs_dir = tmp_path / "logs"
    task_id_file = tmp_path / "task_id.txt"
    job_path = tmp_path / "job.json"

    start_job = {
        "mode": "start",
        "tasks_dir": str(tasks_dir),
        "logs_dir": str(logs_dir),
        "goal": "Read the basic plan price",
        "criteria": [],
        "task_id_file": str(task_id_file),
        "max_steps": 10,
        "script": [
            decision("open_url", params={"url": fixture_site_url + "/products.html"}),
            decision("finish", params={"result": "Basic plan is $9/month"}),
        ],
    }
    assert run_worker(start_job, job_path) == 0
    task_id = task_id_file.read_text(encoding="utf-8").strip()
    db_path = tasks_dir / task_id / "task.db"

    es = EventStore(db_path)
    event_count_after_completion = len(es.all_events(task_id))
    es.close()

    resume_job = {"mode": "resume", "tasks_dir": str(tasks_dir), "logs_dir": str(logs_dir),
                  "task_id": task_id, "max_steps": 10, "script": []}
    assert run_worker(resume_job, job_path) == 0  # AgentLoop.run() sees status != "running" and returns immediately

    es2 = EventStore(db_path)
    assert len(es2.all_events(task_id)) == event_count_after_completion  # no new events appended
    es2.close()
