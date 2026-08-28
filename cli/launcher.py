"""One-command startup orchestration (`browser-agent start` / `status` / `stop`).

This module never redesigns AgentLoop/BatchOrchestrator/WorkflowOrchestrator/routing —
it only answers "are Ollama, the persistent CDP Chrome, and the UI actually up", starts
whichever of those three are missing, and does so without ever reporting success on the
basis of "a process object exists" (that was the bug: `browser-agent browser start` used
to print success as soon as `subprocess.Popen` returned, even if Chrome exited immediately
and CDP never bound). Every PASS in this module is backed by a real HTTP round trip against
the actual endpoint.

Process ownership: PIDs for services *we* start are persisted to a small JSON state file
under `<runtime_dir>/launcher/state.json` so `status`/`stop` can find them across separate
CLI invocations. We only ever act on PIDs we ourselves recorded — never on process names
(no `taskkill /IM chrome.exe`), because a previous version of this tool killed unrelated
Python processes that way.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from agent.config import AppConfig

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765


# ---------------------------------------------------------------------------
# Generic HTTP polling — the one mechanism every "is X actually up" check uses.
# ---------------------------------------------------------------------------

def http_get_ok(url: str, timeout_s: float = 3.0) -> Optional[dict[str, Any]]:
    """Returns the parsed JSON body on HTTP 200, or None on any failure. Never raises."""
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except ValueError:
                return {}
    except httpx.RequestError:
        return None


def poll_until_healthy(
    check: "callable[[], Optional[dict[str, Any]]]",
    timeout_s: float,
    interval_s: float = 0.5,
) -> Optional[dict[str, Any]]:
    """Polls `check` on a bounded schedule instead of sleeping a fixed amount and hoping.
    Returns the first non-None result, or None if the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = check()
        if result is not None:
            return result
        time.sleep(interval_s)
    return check()  # one last try right at the deadline


def process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) doesn't reliably report liveness on Windows; query the process list.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we don't own it
    except OSError:
        return False


def _detached_popen(args: list[str], **kwargs) -> subprocess.Popen:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        args,
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# State / lock file
# ---------------------------------------------------------------------------

@dataclass
class LauncherState:
    chrome_pid: Optional[int] = None
    chrome_started_by_us: bool = False
    chrome_profile_dir: Optional[str] = None
    chrome_cdp_endpoint: Optional[str] = None
    ollama_pid: Optional[int] = None
    ollama_started_by_us: bool = False
    ollama_endpoint: Optional[str] = None
    ui_port: Optional[int] = None
    ui_pid: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chrome_pid": self.chrome_pid,
            "chrome_started_by_us": self.chrome_started_by_us,
            "chrome_profile_dir": self.chrome_profile_dir,
            "chrome_cdp_endpoint": self.chrome_cdp_endpoint,
            "ollama_pid": self.ollama_pid,
            "ollama_started_by_us": self.ollama_started_by_us,
            "ollama_endpoint": self.ollama_endpoint,
            "ui_port": self.ui_port,
            "ui_pid": self.ui_pid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LauncherState":
        return cls(**{k: data.get(k) for k in cls().to_dict().keys()})


class LauncherPaths:
    def __init__(self, runtime_dir: Path):
        self.dir = runtime_dir / "launcher"
        self.state_file = self.dir / "state.json"
        self.lock_file = self.dir / "start.lock"

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)


def load_state(paths: LauncherPaths) -> LauncherState:
    if not paths.state_file.exists():
        return LauncherState()
    try:
        return LauncherState.from_dict(json.loads(paths.state_file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return LauncherState()


def save_state(paths: LauncherPaths, state: LauncherState) -> None:
    paths.ensure_dir()
    paths.state_file.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


class AlreadyRunningError(Exception):
    pass


def acquire_start_lock(paths: LauncherPaths) -> None:
    """Prevents two concurrent `browser-agent start` invocations from racing each other into
    duplicate Chrome/Ollama/UI processes. Recovers automatically from a stale lock file left
    behind by a crashed or killed previous run."""
    paths.ensure_dir()
    if paths.lock_file.exists():
        try:
            old_pid = int(paths.lock_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and process_alive(old_pid):
            raise AlreadyRunningError(
                f"Another `browser-agent start` is already running (pid {old_pid}).\n"
                "If that process is gone but this message persists, delete:\n"
                f"  {paths.lock_file}"
            )
        # stale lock — the recorded PID is dead, safe to reclaim.
    paths.lock_file.write_text(str(os.getpid()), encoding="utf-8")


def release_start_lock(paths: LauncherPaths) -> None:
    try:
        if paths.lock_file.exists() and paths.lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            paths.lock_file.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Result type shared by every "ensure X" step
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    ok: bool
    summary: str
    detail: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _ollama_candidates() -> list[str]:
    found = shutil.which("ollama") or shutil.which("ollama.exe")
    candidates = [found] if found else []
    candidates += [
        str(Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"),
        r"C:\Program Files\Ollama\ollama.exe",
        r"C:\Program Files (x86)\Ollama\ollama.exe",
    ]
    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen and Path(c).exists():
            seen.add(c)
            result.append(c)
    return result


def check_ollama(endpoint: str) -> Optional[dict[str, Any]]:
    return http_get_ok(f"{endpoint.rstrip('/')}/api/tags", timeout_s=3.0)


def ensure_ollama(endpoint: str, model_name: str, start_timeout_s: float = 30.0) -> tuple[StepResult, Optional[int]]:
    """Returns (result, pid_if_we_started_it)."""
    tags = check_ollama(endpoint)
    started_pid: Optional[int] = None
    if tags is None:
        exe = next(iter(_ollama_candidates()), None)
        if exe is None:
            return StepResult(
                False, "Ollama unavailable",
                f"Could not reach {endpoint}/api/tags and could not find ollama.exe on this machine.\n"
                "Install Ollama or start it manually, then re-run `browser-agent start`.",
            ), None
        proc = _detached_popen([exe, "serve"])
        started_pid = proc.pid
        tags = poll_until_healthy(lambda: check_ollama(endpoint), timeout_s=start_timeout_s, interval_s=0.5)
        if tags is None:
            return StepResult(
                False, "Ollama failed to start",
                f"Launched {exe} serve (pid {started_pid}) but {endpoint}/api/tags never became "
                f"reachable within {start_timeout_s:.0f}s.",
                diagnostics={"executable": exe, "pid": started_pid, "endpoint": endpoint},
            ), started_pid

    models = [m.get("name", "") for m in tags.get("models", [])]
    model_present = any(m == model_name or m.split(":")[0] == model_name.split(":")[0] for m in models)
    if not model_present:
        return StepResult(
            False, f"{model_name} is not installed",
            f"Run:\n  ollama pull {model_name}\n\nInstalled models: {', '.join(models) or '(none)'}",
        ), started_pid

    return StepResult(True, "Ollama", f"{model_name} available"), started_pid


def query_gpu_info(endpoint: str) -> Optional[str]:
    """Best-effort GPU telemetry via `ollama ps`. Never fails startup if unavailable —
    Ollama owns GPU/CPU placement, this is display-only."""
    data = http_get_ok(f"{endpoint.rstrip('/')}/api/ps", timeout_s=3.0)
    if not data:
        return None
    models = data.get("models", [])
    if not models:
        return None
    m = models[0]
    size_vram = m.get("size_vram")
    size = m.get("size")
    if size_vram and size and size_vram > 0:
        pct = (size_vram / size) * 100 if size else 0
        return f"GPU offload {pct:.0f}% ({m.get('name', '')})"
    return None


# ---------------------------------------------------------------------------
# Chrome / CDP
# ---------------------------------------------------------------------------

def check_cdp(endpoint: str) -> Optional[dict[str, Any]]:
    return http_get_ok(f"{endpoint.rstrip('/')}/json/version", timeout_s=3.0)


def find_chrome_executable() -> Optional[str]:
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


def ensure_chrome_cdp(
    endpoint: str,
    profile_dir: Path,
    start_timeout_s: float = 20.0,
) -> tuple[StepResult, Optional[int]]:
    """Returns (result, pid_if_we_started_it). Never reports PASS on process creation alone —
    only after `endpoint/json/version` actually responds."""
    info = check_cdp(endpoint)
    if info is not None:
        return StepResult(True, "Persistent Chrome", f"CDP {endpoint} (reused existing)"), None

    chrome = find_chrome_executable()
    if chrome is None:
        return StepResult(
            False, "Chrome not found",
            "Could not find chrome.exe automatically. Start it manually with:\n"
            f'  chrome.exe --remote-debugging-port={endpoint.rsplit(":", 1)[-1]} '
            f'--remote-debugging-address=127.0.0.1 --user-data-dir="{profile_dir}" --no-first-run',
        ), None

    profile_dir.mkdir(parents=True, exist_ok=True)
    port = endpoint.rsplit(":", 1)[-1]
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
    ]
    proc = _detached_popen(args)
    info = poll_until_healthy(lambda: check_cdp(endpoint), timeout_s=start_timeout_s, interval_s=0.5)
    if info is None:
        exit_code = proc.poll()
        return StepResult(
            False, "Persistent Chrome failed to start",
            f"Launched Chrome (pid {proc.pid}) but {endpoint}/json/version never became reachable "
            f"within {start_timeout_s:.0f}s.\n"
            f"  executable:  {chrome}\n"
            f"  profile dir: {profile_dir}\n"
            f"  endpoint:    {endpoint}\n"
            f"  process exit code: {exit_code if exit_code is not None else '(still running, but not serving CDP)'}",
            diagnostics={"executable": chrome, "pid": proc.pid, "exit_code": exit_code, "profile_dir": str(profile_dir)},
        ), proc.pid

    return StepResult(True, "Persistent Chrome", f"CDP {endpoint}"), proc.pid


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def check_ui(host: str, port: int) -> Optional[dict[str, Any]]:
    result = http_get_ok(f"http://{host}:{port}/api/browser/status", timeout_s=3.0)
    return result if result is not None else None


def probe_ui_owner(host: str, port: int) -> tuple[bool, bool]:
    """Returns (port_in_use, is_browseragent). A plain TCP probe distinguishes "nothing is
    listening" from "something is listening but isn't us" without assuming our own health
    endpoint is what answers."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((host, port))
        except OSError:
            return False, False
    return True, check_ui(host, port) is not None


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def stop_pid(pid: int, force: bool = False) -> bool:
    """Stops exactly one PID we ourselves recorded — never a process-name sweep (that's what
    broke unrelated Python processes before). Returns True if a stop was issued.

    On Windows, a plain `taskkill /PID` only works by posting WM_CLOSE to a window the target
    process owns. BrowserAgent's own UI server runs with no console/window (started detached,
    hidden), so a graceful taskkill always fails there with "can only be terminated forcefully"
    — confirmed live on this machine. Falling back to `/F` on that specific failure means Stop
    still actually stops the process instead of silently no-op'ing and leaving the port held.
    """
    if not process_alive(pid):
        return False
    if sys.platform == "win32":
        args = ["taskkill", "/PID", str(pid)]
        if force:
            args.append("/F")
        result = subprocess.run(args, capture_output=True, timeout=10)
        if result.returncode != 0 and not force:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
        return True
    import signal

    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    return True


def format_step(result: StepResult) -> str:
    label = "PASS" if result.ok else "FAIL"
    lines = [f"[{label}] {result.summary}"]
    if result.detail:
        for line in result.detail.splitlines():
            lines.append(f"       {line}")
    return "\n".join(lines)
