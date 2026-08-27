"""Phase 5B assignment-sweep-through-the-UI-router live validation (Section 35): submits a
plain-English prompt (with pasted target URLs, exactly as a user would paste into the UI
prompt box) through the SAME router + BatchOrchestrator path ui/jobs.py uses — no manual
`browser-agent batch run` — against the existing Phase 5 multisite assignment fixture
generator, and checks precision/recall against its ground truth.

Usage:
    python benchmarks/run_phase5b_assignment_sweep_live.py [--count 6] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # `tests` isn't part of the installed editable package

from agent.config import load_config
from batch.models import ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from inference.llama_client import create_inference_client
from router.policy import route, to_batch_policy
from tests.fixtures.multisite.generate_multisite import generate_assignment_fixture


def _assignment_contract() -> ResultContract:
    from cli.main import _contract_from_name
    return _contract_from_name("assignment")


def _start_server(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / f"phase5b_assignment_sweep_{time.strftime('%Y%m%d_%H%M%S')}"))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir = output_dir / "fixture"
    truth = generate_assignment_fixture(fixture_dir, args.count)

    server, base_url = _start_server(fixture_dir)
    try:
        urls = [f"{base_url}/{t}" for t in truth["targets"]]
        prompt = (
            "Check these pages and tell me every assignment I still have to do:\n"
            + "\n".join(urls)
        )

        config = load_config(args.config)
        config.browser.headless = True
        config.browser.interactive_approval = False
        config.storage.tasks_dir = str(output_dir / "tasks")
        config.browser.user_data_dir = str(output_dir / "tasks")
        config.logging.dir = str(output_dir / "logs")

        client = create_inference_client(config)
        decision = await route(prompt, client)  # exercises the real natural-language router
        assert decision.task_type.value == "multisite_sweep", f"expected multisite_sweep, got {decision.task_type.value}"
        assert decision.result_contract == "assignment", f"expected assignment contract, got {decision.result_contract!r}"
        assert set(decision.targets) == set(urls), "router must preserve exactly the pasted URLs"

        store = BatchStore.for_batch_dir(output_dir / "batch")
        contract = _assignment_contract()
        policy = to_batch_policy(decision, max_steps_per_item=15)
        batch_id = store.create_batch(decision.objective, decision.targets, contract, policy)
        started = time.monotonic()
        final = await BatchOrchestrator(config, store, batch_id, policy, contract).run()
        elapsed_s = time.monotonic() - started
        store.close()

        found_evidence = {
            (f["finding"].get("course", ""), f["finding"].get("title", ""))
            for f in final["findings"]
        }
        truth_pairs = {(a["course"], a["assignment"]) for a in truth["assignments"]}
        true_positive = len(found_evidence & truth_pairs)
        precision = true_positive / len(found_evidence) if found_evidence else (1.0 if not truth_pairs else 0.0)
        recall = true_positive / len(truth_pairs) if truth_pairs else (1.0 if not found_evidence else 0.0)

        summary = {
            "prompt": prompt,
            "routed_as": decision.task_type.value,
            "targets": len(urls),
            "completed": final["completed"],
            "failed": final["failed"],
            "ground_truth_assignments": len(truth_pairs),
            "found_findings": len(found_evidence),
            "true_positive": true_positive,
            "precision": precision,
            "recall": recall,
            "duration_s": elapsed_s,
        }
        with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
            json.dump({**summary, "final": final}, f, indent=2)
        print(json.dumps(summary, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
