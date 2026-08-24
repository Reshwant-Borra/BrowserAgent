"""Shared pytest fixtures: the static fixture-site HTTP server and a throwaway AppConfig
pointed at a temp directory. Uses stdlib http.server (ThreadingHTTPServer) — no framework —
since every fixture-site scenario (delayed nav, no-op, redirect loop, modal) is achievable
with static HTML + client-side JS."""
from __future__ import annotations

import functools
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from agent.config import AppConfig, BrowserConfig, ContextConfig, LoggingConfig, ModelConfig, RecoveryConfig, StorageConfig

FIXTURE_SITE_DIR = Path(__file__).resolve().parent / "fixtures" / "simple_site"


@pytest.fixture(scope="session")
def fixture_site_url():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_SITE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def tmp_config(tmp_path) -> AppConfig:
    return AppConfig(
        model=ModelConfig(endpoint="http://127.0.0.1:1", temperature=0.1, max_output_tokens=256,
                           max_output_tokens_deep_recovery=512, request_timeout_s=5.0),
        browser=BrowserConfig(headless=True, user_data_dir=str(tmp_path / "tasks"),
                               action_timeout_ms=5000, interactive_approval=False),
        context=ContextConfig(max_page_chars=3000, max_page_chars_deep_recovery=6000,
                               recent_actions=5, max_visible_text_items=12),
        recovery=RecoveryConfig(max_action_retries=2, identical_action_limit=3,
                                 navigation_cycle_limit=2, verification_retry_limit=2),
        storage=StorageConfig(tasks_dir=str(tmp_path / "tasks")),
        logging=LoggingConfig(level="INFO", dir=str(tmp_path / "logs"), redact_secrets=True),
    )
