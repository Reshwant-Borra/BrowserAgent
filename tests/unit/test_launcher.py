"""Deterministic tests for cli/launcher.py (one-command startup orchestration).

Nothing here talks to a real Ollama, Chrome, or network socket — every external boundary
(HTTP, subprocess, process liveness) is monkeypatched. The regression this suite exists to
pin down: a startup step may only report PASS after the real endpoint responds; process
creation succeeding is never sufficient (that was the bug in the old
`browser-agent browser start` — see cli/main.py's cmd_browser_start docstring).
"""
from __future__ import annotations

import json

import pytest

from cli import launcher


class _FakeProc:
    def __init__(self, pid: int, exit_code=None):
        self.pid = pid
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def test_ollama_healthy_is_reused_without_starting_a_process(monkeypatch):
    monkeypatch.setattr(launcher, "http_get_ok", lambda url, timeout_s=3.0: {"models": [{"name": "qwen3:8b"}]})
    started = []
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: started.append(args) or _FakeProc(999))

    result, pid = launcher.ensure_ollama("http://127.0.0.1:11434", "qwen3:8b")

    assert result.ok
    assert pid is None
    assert started == []


def test_ollama_unavailable_gets_started_and_polled_healthy(monkeypatch):
    calls = {"n": 0}

    def fake_check(endpoint):
        calls["n"] += 1
        if calls["n"] < 3:
            return None
        return {"models": [{"name": "qwen3:8b"}]}

    monkeypatch.setattr(launcher, "check_ollama", fake_check)
    monkeypatch.setattr(launcher, "_ollama_candidates", lambda: [r"C:\ollama\ollama.exe"])
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: _FakeProc(4242))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    result, pid = launcher.ensure_ollama("http://127.0.0.1:11434", "qwen3:8b", start_timeout_s=5.0)

    assert result.ok
    assert pid == 4242
    assert calls["n"] >= 3


def test_ollama_start_timeout_reports_failure_not_success(monkeypatch):
    monkeypatch.setattr(launcher, "check_ollama", lambda endpoint: None)
    monkeypatch.setattr(launcher, "_ollama_candidates", lambda: [r"C:\ollama\ollama.exe"])
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: _FakeProc(4242))
    monkeypatch.setattr(launcher.time, "monotonic", _monotonic_stub(step=1.0))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    result, pid = launcher.ensure_ollama("http://127.0.0.1:11434", "qwen3:8b", start_timeout_s=2.0)

    assert not result.ok
    assert "never became reachable" in result.detail
    assert pid == 4242


def test_ollama_missing_when_not_found_on_machine(monkeypatch):
    monkeypatch.setattr(launcher, "check_ollama", lambda endpoint: None)
    monkeypatch.setattr(launcher, "_ollama_candidates", lambda: [])

    result, pid = launcher.ensure_ollama("http://127.0.0.1:11434", "qwen3:8b")

    assert not result.ok
    assert pid is None
    assert "could not find ollama.exe" in result.detail.lower()


def test_missing_model_reports_pull_instruction(monkeypatch):
    monkeypatch.setattr(launcher, "check_ollama", lambda endpoint: {"models": [{"name": "llama3:8b"}]})

    result, pid = launcher.ensure_ollama("http://127.0.0.1:11434", "qwen3:8b")

    assert not result.ok
    assert pid is None
    assert "ollama pull qwen3:8b" in result.detail


def _monotonic_stub(step: float):
    state = {"t": 0.0}

    def _fn():
        state["t"] += step
        return state["t"]

    return _fn


# ---------------------------------------------------------------------------
# Chrome / CDP
# ---------------------------------------------------------------------------

def test_cdp_healthy_is_reused_without_launching_chrome(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "check_cdp", lambda endpoint: {"Browser": "Chrome/1.0"})
    launched = []
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: launched.append(args) or _FakeProc(1))

    result, pid = launcher.ensure_chrome_cdp("http://127.0.0.1:9222", tmp_path)

    assert result.ok
    assert "reused" in result.detail
    assert pid is None
    assert launched == []


def test_chrome_starts_and_cdp_becomes_healthy(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_check(endpoint):
        calls["n"] += 1
        return None if calls["n"] < 2 else {"Browser": "Chrome/1.0"}

    monkeypatch.setattr(launcher, "check_cdp", fake_check)
    monkeypatch.setattr(launcher, "find_chrome_executable", lambda: r"C:\chrome\chrome.exe")
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: _FakeProc(555))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    result, pid = launcher.ensure_chrome_cdp("http://127.0.0.1:9222", tmp_path, start_timeout_s=5.0)

    assert result.ok
    assert pid == 555


def test_chrome_process_starts_but_cdp_never_appears_is_failure(monkeypatch, tmp_path):
    """The exact regression this launcher exists to prevent: a process object existing must
    never be reported as success. Only a real /json/version response counts."""
    monkeypatch.setattr(launcher, "check_cdp", lambda endpoint: None)
    monkeypatch.setattr(launcher, "find_chrome_executable", lambda: r"C:\chrome\chrome.exe")
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: _FakeProc(777, exit_code=None))
    monkeypatch.setattr(launcher.time, "monotonic", _monotonic_stub(step=1.0))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    result, pid = launcher.ensure_chrome_cdp("http://127.0.0.1:9222", tmp_path, start_timeout_s=2.0)

    assert not result.ok
    assert pid == 777
    assert "never became reachable" in result.detail
    assert "profile dir" in result.detail.lower()
    assert "endpoint" in result.detail.lower()


def test_chrome_exits_early_reports_exit_code_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "check_cdp", lambda endpoint: None)
    monkeypatch.setattr(launcher, "find_chrome_executable", lambda: r"C:\chrome\chrome.exe")
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: _FakeProc(888, exit_code=1))
    monkeypatch.setattr(launcher.time, "monotonic", _monotonic_stub(step=1.0))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)

    result, pid = launcher.ensure_chrome_cdp("http://127.0.0.1:9222", tmp_path, start_timeout_s=2.0)

    assert not result.ok
    assert result.diagnostics["exit_code"] == 1


def test_chrome_not_found_fails_without_attempting_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "check_cdp", lambda endpoint: None)
    monkeypatch.setattr(launcher, "find_chrome_executable", lambda: None)
    launched = []
    monkeypatch.setattr(launcher, "_detached_popen", lambda args, **kw: launched.append(args))

    result, pid = launcher.ensure_chrome_cdp("http://127.0.0.1:9222", tmp_path)

    assert not result.ok
    assert pid is None
    assert launched == []


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def test_ui_reused_when_port_already_serving_browseragent(monkeypatch):
    monkeypatch.setattr(launcher, "check_ui", lambda host, port: {"mode": "cdp_attach", "connected": True})

    class FakeSocket:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def connect(self, addr): pass

    import socket as socket_mod
    monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: FakeSocket())

    in_use, is_ours = launcher.probe_ui_owner("127.0.0.1", 8765)
    assert in_use is True
    assert is_ours is True


def test_ui_port_occupied_by_non_browseragent_service(monkeypatch):
    monkeypatch.setattr(launcher, "check_ui", lambda host, port: None)

    class FakeSocket:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def connect(self, addr): pass

    import socket as socket_mod
    monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: FakeSocket())

    in_use, is_ours = launcher.probe_ui_owner("127.0.0.1", 8765)
    assert in_use is True
    assert is_ours is False


def test_ui_not_running_when_port_closed(monkeypatch):
    class FakeSocket:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def connect(self, addr): raise OSError("refused")

    import socket as socket_mod
    monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: FakeSocket())

    in_use, is_ours = launcher.probe_ui_owner("127.0.0.1", 8765)
    assert in_use is False
    assert is_ours is False


# ---------------------------------------------------------------------------
# Lock file / duplicate-start prevention
# ---------------------------------------------------------------------------

def test_lock_prevents_concurrent_start(tmp_path, monkeypatch):
    paths = launcher.LauncherPaths(tmp_path)
    launcher.acquire_start_lock(paths)

    monkeypatch.setattr(launcher, "process_alive", lambda pid: True)
    with pytest.raises(launcher.AlreadyRunningError):
        launcher.acquire_start_lock(paths)

    launcher.release_start_lock(paths)
    assert not paths.lock_file.exists()


def test_stale_lock_is_recovered_automatically(tmp_path, monkeypatch):
    paths = launcher.LauncherPaths(tmp_path)
    paths.ensure_dir()
    paths.lock_file.write_text("999999", encoding="utf-8")

    monkeypatch.setattr(launcher, "process_alive", lambda pid: False)
    launcher.acquire_start_lock(paths)  # must not raise

    assert paths.lock_file.read_text(encoding="utf-8").strip() == str(__import__("os").getpid())


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_state_round_trips_through_disk(tmp_path):
    paths = launcher.LauncherPaths(tmp_path)
    state = launcher.LauncherState(chrome_pid=111, chrome_started_by_us=True, ui_pid=222, ui_port=8765)
    launcher.save_state(paths, state)

    loaded = launcher.load_state(paths)
    assert loaded.chrome_pid == 111
    assert loaded.chrome_started_by_us is True
    assert loaded.ui_pid == 222
    assert loaded.ui_port == 8765


def test_corrupt_state_file_falls_back_to_empty(tmp_path):
    paths = launcher.LauncherPaths(tmp_path)
    paths.ensure_dir()
    paths.state_file.write_text("{not json", encoding="utf-8")

    loaded = launcher.load_state(paths)
    assert loaded.chrome_pid is None


# ---------------------------------------------------------------------------
# stop_pid never uses a broad process-name kill
# ---------------------------------------------------------------------------

def test_stop_pid_targets_only_the_given_pid(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(launcher, "process_alive", lambda pid: True)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    did_stop = launcher.stop_pid(4321)

    assert did_stop is True
    assert captured["args"] == ["taskkill", "/PID", "4321"]
    assert "/IM" not in captured["args"]


def test_stop_pid_falls_back_to_force_when_graceful_taskkill_fails(monkeypatch):
    """Regression: a plain `taskkill /PID` cannot close a windowless background process (e.g.
    BrowserAgent's own detached UI server) on Windows — confirmed live, it returns
    "can only be terminated forcefully". Without a fallback, Stop silently no-ops and the
    process (and the port it holds) never actually goes away."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))

        class R:
            returncode = 0 if "/F" in args else 1

        return R()

    monkeypatch.setattr(launcher, "process_alive", lambda pid: True)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    did_stop = launcher.stop_pid(4321)

    assert did_stop is True
    assert calls == [["taskkill", "/PID", "4321"], ["taskkill", "/PID", "4321", "/F"]]


def test_stop_pid_is_noop_when_process_already_dead(monkeypatch):
    monkeypatch.setattr(launcher, "process_alive", lambda pid: False)
    called = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: called.append(a))

    did_stop = launcher.stop_pid(4321)

    assert did_stop is False
    assert called == []


# ---------------------------------------------------------------------------
# GPU telemetry is best-effort and never fails startup
# ---------------------------------------------------------------------------

def test_gpu_info_absent_returns_none_without_raising(monkeypatch):
    monkeypatch.setattr(launcher, "http_get_ok", lambda url, timeout_s=3.0: None)
    assert launcher.query_gpu_info("http://127.0.0.1:11434") is None


def test_gpu_info_parses_vram_offload(monkeypatch):
    monkeypatch.setattr(
        launcher, "http_get_ok",
        lambda url, timeout_s=3.0: {"models": [{"name": "qwen3:8b", "size": 1000, "size_vram": 1000}]},
    )
    info = launcher.query_gpu_info("http://127.0.0.1:11434")
    assert info is not None
    assert "100%" in info
