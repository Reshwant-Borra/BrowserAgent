"""Phase 5B router live-fallback slice (Section 64-65): runs genuinely-ambiguous prompts
(no URLs, not obviously research-shaped — the only bucket `router/extract.py`'s deterministic
rules correctly defer on) through the real Qwen3-8B/Ollama fallback path
(`router/llm_router.route_with_model`), checking schema validity and the no-hallucinated-
targets guarantee. This is the REDUCED live slice (~15-20 prompts); the full 50-prompt
tuned+holdout gate (Section 64) is mostly satisfied deterministically already (see
tests/unit/test_router.py) — this script exists specifically to exercise the live-model path,
which only ever fires for the ambiguous bucket by design (Section 10).

Usage:
    python benchmarks/run_phase5b_router_live.py [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from agent.config import load_config
from inference.llama_client import create_inference_client
from router.extract import try_deterministic_route
from router.llm_router import RouterOutputError, route_with_model

ROOT = Path(__file__).resolve().parent.parent

# All deliberately ambiguous: no URLs, no explicit research verbs — exactly the bucket
# router/extract.py's try_deterministic_route() correctly returns None for (Section 10).
PROMPTS = [
    "Check my school stuff.",
    "Handle the usual thing for me.",
    "Do the weekly check.",
    "Take care of my accounts.",
    "See if anything needs attention.",
    "Look into the situation and let me know.",
    "Follow up on the pending items.",
    "Deal with my inbox situation.",
    "Sort out what's outstanding.",
    "Give me an update on things.",
    "Look at what's going on and report back.",
    "Take a look and tell me what you find.",
    "Handle it the way you normally would.",
    "Check in on my usual sites.",
    "See what's new since last time.",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / f"phase5b_router_live_{time.strftime('%Y%m%d_%H%M%S')}"))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    client = create_inference_client(config)

    if not await client.health_check():
        print(f"Local model endpoint unavailable: {client.endpoint}")
        raise SystemExit(1)

    results = []
    for prompt in PROMPTS:
        assert try_deterministic_route(prompt) is None, f"prompt should have been ambiguous: {prompt!r}"
        started = time.monotonic()
        try:
            decision = await route_with_model(client, prompt)
            elapsed_ms = (time.monotonic() - started) * 1000
            results.append({
                "prompt": prompt,
                "ok": True,
                "task_type": decision.task_type.value,
                "targets": decision.targets,
                "requires_discovery": decision.requires_discovery,
                "preferred_policy": decision.preferred_policy.value,
                "latency_ms": elapsed_ms,
            })
        except RouterOutputError as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            results.append({"prompt": prompt, "ok": False, "error": str(exc), "latency_ms": elapsed_ms})

    schema_valid = sum(1 for r in results if r["ok"])
    no_hallucinated_targets = sum(1 for r in results if r["ok"] and not r["targets"])
    summary = {
        "total": len(results),
        "schema_valid": schema_valid,
        "schema_valid_rate": schema_valid / len(results),
        "no_hallucinated_targets": no_hallucinated_targets,
        "results": results,
    }
    with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    for r in results:
        print(f"[{'OK' if r['ok'] else 'FAIL'}] {r['prompt']!r} -> "
              f"{r.get('task_type', r.get('error'))} targets={r.get('targets')}")


if __name__ == "__main__":
    asyncio.run(main())
