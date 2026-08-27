"""Phase 5B research-discovery validation (corrective pass): runs research/discovery.py's
deterministic candidate-link enumeration + Qwen3-8B id-selection against a controlled local
search-results fixture (tests/fixtures/simple_site/search_results.html) rather than live
DuckDuckGo — isolates the enumeration/selection reliability question from search-engine
network flakiness. See docs/PHASE5B_REPORT.md for the follow-up live-DuckDuckGo-through-the-
UI attempt.

Usage:
    python benchmarks/run_phase5b_research_discovery_live.py [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.config import load_config
from inference.llama_client import create_inference_client
from research.discovery import discover_sources

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SITE_DIR = ROOT / "tests" / "fixtures" / "simple_site"

RELEVANT_URLS = {
    "https://example.edu/pomodoro-technique-guide",
    "https://example.org/study-methods-compared",
    "https://example.io/research-focus-productivity",
}
IRRELEVANT_URLS = {
    "https://shop.example.com/kitchen-timers",
    "https://example.net/time-management-basics",
}
ALL_KNOWN_URLS = RELEVANT_URLS | IRRELEVANT_URLS


def _start_fixture_server() -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_SITE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "runtime" / "benchmark_runs" / f"phase5b_research_discovery_{time.strftime('%Y%m%d_%H%M%S')}"),
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(args.config)
    config.browser.headless = True
    config.browser.interactive_approval = False
    config.storage.tasks_dir = str(output_dir / "tasks")
    config.browser.user_data_dir = str(output_dir / "tasks")
    config.logging.dir = str(output_dir / "logs")

    client = create_inference_client(config)
    server, base_url = _start_fixture_server()
    try:
        search_url = f"{base_url}/search_results.html"
        started = time.monotonic()
        urls = await discover_sources(
            config,
            client,
            "the benefits of the Pomodoro technique for studying",
            output_dir / "discovery_browser_profile",
            max_sources=10,
            search_engine_url=search_url,
        )
        elapsed_s = time.monotonic() - started

        relevant_selected = [u for u in urls if u in RELEVANT_URLS]
        irrelevant_selected = [u for u in urls if u in IRRELEVANT_URLS]
        hallucinated = [u for u in urls if u not in ALL_KNOWN_URLS]
        record = {
            "selected_urls": urls,
            "selected_count": len(urls),
            "relevant_selected": relevant_selected,
            "irrelevant_selected": irrelevant_selected,
            "hallucinated_urls": hallucinated,
            "multi_source_enumeration": len(urls) >= 2,
            "zero_hallucinated_urls": len(hallucinated) == 0,
            "duration_s": elapsed_s,
        }
        (output_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps(record, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
