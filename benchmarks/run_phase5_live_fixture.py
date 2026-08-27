from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import statistics
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from batch.models import BatchPolicy, NavigationScope, ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore
from inference.llama_client import create_inference_client
from tests.fixtures.multisite.generate_multisite import generate_assignment_fixture, generate_research_fixture


def _start_server(root: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


async def run_live(kind: str, count: int, output_dir: Path, max_steps: int, max_seconds: float) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir = output_dir / "site"
    truth = generate_assignment_fixture(fixture_dir, count) if kind == "assignment" else generate_research_fixture(fixture_dir, count)
    server, base_url = _start_server(fixture_dir)
    try:
        targets = [f"{base_url}/{target}" for target in truth["targets"]]
        config = load_config(None)
        config.browser.headless = True
        config.browser.interactive_approval = False
        config.storage.runtime_dir = str(output_dir / "runtime")
        config.storage.tasks_dir = str(output_dir / "runtime" / "tasks")
        config.browser.user_data_dir = str(output_dir / "runtime" / "tasks")
        config.logging.dir = str(output_dir / "runtime" / "logs")
        config.model.request_timeout_s = max(float(config.model.request_timeout_s), max_seconds)
        client = create_inference_client(config)
        if not await client.health_check():
            raise RuntimeError(f"model backend unavailable at {client.endpoint}")
        contract = _contract(kind)
        policy = BatchPolicy(
            work_item_max_attempts=1,
            max_steps_per_item=max_steps,
            max_seconds_per_item=max_seconds,
            read_only=True,
            navigation_scope=NavigationScope.SAME_ORIGIN,
        )
        batch_id = f"live_{kind}_{count}"
        store = BatchStore.for_batch_dir(output_dir / "runtime" / "batches" / batch_id)
        try:
            store.create_batch(f"Phase 5 live {kind} sweep", targets, contract, policy, batch_id=batch_id)
            rss = {"startup": _process_rss_mb()}
            rss_checkpoints: dict[str, float | None] = {}

            def record_rss(item: dict[str, Any]) -> None:
                ordinal = int(item["ordinal"])
                if ordinal in {10, 25, 50, 100}:
                    rss_checkpoints[str(ordinal)] = _process_rss_mb()

            start = time.monotonic()
            final = await BatchOrchestrator(
                config,
                store,
                batch_id,
                policy,
                contract,
                item_completed_callback=record_rss,
            ).run()
            duration = time.monotonic() - start
            rss.update(_rss_checkpoints(store, batch_id, rss_checkpoints))
            rss["peak"] = max(v for v in rss.values() if v is not None) if any(v is not None for v in rss.values()) else None
            evaluation = _evaluate(kind, truth, store, batch_id)
            metrics = _metrics(config, store, batch_id)
            summary = {
                "kind": kind,
                "targets": count,
                "batch_id": batch_id,
                "duration_s": duration,
                "queue": {
                    "completed": final["completed"],
                    "failed": final["failed"],
                    "blocked": final["blocked"],
                    "raw_findings": final["raw_findings"],
                    "deduplicated_findings": final["deduplicated_findings"],
                    "status": final["status"],
                },
                "evaluation": evaluation,
                "metrics": metrics,
                "rss_mb": rss,
                "final": final,
            }
            (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
            return summary
        finally:
            store.close()
    finally:
        server.shutdown()
        server.server_close()


def _contract(kind: str) -> ResultContract:
    if kind == "assignment":
        return ResultContract(
            name="assignment",
            description="Find actionable upcoming assignments only; include course, assignment title, due date if present, status, source URL, and evidence.",
            required_fields=["course", "title", "due_date", "status", "actionable", "source_url", "evidence"],
            field_definitions={
                "status": "one of upcoming, current_incomplete, completed, closed, past_archived, unknown",
                "actionable": "true only when the assignment still requires action",
            },
        )
    return ResultContract(
        name="research",
        description="Classify relevance and extract requested research fields with source URL and evidence.",
        required_fields=["pricing", "education_discount", "public_api_docs", "source_url", "evidence"],
        field_definitions={
            "pricing": "pricing, plan, cost, seat, or monthly price information",
            "education_discount": "education, school, student, teacher, or academic discount information",
            "public_api_docs": "public API, REST API, developer documentation, or API docs availability",
        },
    )


def _evaluate(kind: str, truth: dict[str, Any], store: BatchStore, batch_id: str) -> dict[str, Any]:
    results = [dict(row) for row in store.results(batch_id)]
    predictions = []
    for row in results:
        data = json.loads(row["structured_data"])
        for finding in data.get("findings", []):
            if isinstance(finding, dict):
                predictions.append({**finding, "work_item_id": row["work_item_id"], "result_id": row["id"]})
    if kind == "assignment":
        predictions = [p for p in predictions if p.get("actionable") is True]
        truth_keys = {
            _assignment_key(row["course"], row["assignment"], row["due_date"])
            for row in truth["assignments"]
        }
        pred_keys = {
            _assignment_key(
                str(p.get("course") or ""),
                str(p.get("title") or p.get("assignment") or ""),
                str(p.get("value") or p.get("due_date") or ""),
            )
            for p in predictions
        }
    else:
        truth_keys = {
            _research_key(row["source"], _research_field_for_fact(row["fact"]))
            for row in truth["facts"]
        }
        target_by_item = {
            row["id"]: Path(row["target"]).name
            for row in store.items(batch_id)
        }
        pred_keys = {
            _research_key(target_by_item.get(p["work_item_id"], ""), str(p.get("field") or ""))
            for p in predictions
            if p.get("field") and p.get("value") and p.get("evidence")
        }
    tp = len(truth_keys & pred_keys)
    fp = len(pred_keys - truth_keys)
    fn = len(truth_keys - pred_keys)
    precision = tp / (tp + fp) if tp + fp else 1.0 if not truth_keys else 0.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "truth_count": len(truth_keys),
        "predicted_count": len(pred_keys),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": (2 * precision * recall / (precision + recall)) if precision + recall else 0.0,
        "missing_keys": sorted(truth_keys - pred_keys),
        "extra_keys": sorted(pred_keys - truth_keys),
        **(_research_field_breakdown(truth, predictions, store, batch_id) if kind == "research" else {}),
    }


def _metrics(config, store: BatchStore, batch_id: str) -> dict[str, Any]:
    prompt_tokens = []
    calls_by_item = []
    actions_by_item = []
    durations_ms = []
    for item in store.items(batch_id):
        task_id = item["browser_task_id"]
        if not task_id:
            continue
        rows = _read_jsonl(Path(config.logging.dir) / f"{task_id}.metrics.jsonl")
        model_calls = [r for r in rows if r.get("event") == "model_call"]
        actions = [r for r in rows if r.get("event") == "action"]
        prompt_tokens.extend([
            r.get("prompt_tokens") or r.get("total_estimated_prompt_tokens")
            for r in model_calls
            if (r.get("prompt_tokens") or r.get("total_estimated_prompt_tokens")) is not None
        ])
        calls_by_item.append(len(model_calls))
        actions_by_item.append(len(actions))
        if item["started_at"] and item["completed_at"]:
            # SQLite timestamps are ISO strings; exact duration is less important than per-run wall time.
            durations_ms.append(None)
    checkpoints = {}
    for ordinal in (1, 10, 25, 50, 100):
        item = next((row for row in store.items(batch_id) if row["ordinal"] == ordinal), None)
        if item and item["browser_task_id"]:
            rows = _read_jsonl(Path(config.logging.dir) / f"{item['browser_task_id']}.metrics.jsonl")
            toks = [
                r.get("prompt_tokens") or r.get("total_estimated_prompt_tokens")
                for r in rows if r.get("event") == "model_call"
            ]
            checkpoints[str(ordinal)] = toks[0] if toks else None
    return {
        "model_calls_total": sum(calls_by_item),
        "model_calls_per_item_avg": statistics.mean(calls_by_item) if calls_by_item else 0,
        "actions_per_item_avg": statistics.mean(actions_by_item) if actions_by_item else 0,
        "prompt_tokens_avg": statistics.mean(prompt_tokens) if prompt_tokens else None,
        "prompt_tokens_median": statistics.median(prompt_tokens) if prompt_tokens else None,
        "prompt_tokens_p95": _p95(prompt_tokens),
        "prompt_tokens_max": max(prompt_tokens) if prompt_tokens else None,
        "prompt_token_checkpoints": checkpoints,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _assignment_key(course: str, title: str, due: str) -> str:
    return "|".join([_norm(course), _norm(title), _norm_due(due)])


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _norm_due(value: str) -> str:
    normalized = _norm(value)
    if not normalized or "to be announced" in normalized:
        return ""
    normalized = normalized.removeprefix("due ").removeprefix("deadline: ").removeprefix("submit by ").strip()
    normalized = normalized.split(" at ", 1)[0]
    normalized = normalized.replace("sept ", "sep ")
    import re

    month_match = re.fullmatch(r"(?:september|sep)\s+(\d{1,2})(?:,\s*(\d{4}))?", normalized)
    if month_match:
        return f"sep {int(month_match.group(1))}"
    return normalized


def _p95(values: list[int | float]) -> int | float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return ordered[idx]


def _research_key(source: str, field: str) -> str:
    return f"{Path(source).name}|{field}"


def _research_field_for_fact(fact: str) -> str:
    text = _norm(fact)
    if "pricing" in text or "seat" in text:
        return "pricing"
    if "education" in text or "discount" in text:
        return "education_discount"
    if "api" in text:
        return "public_api_docs"
    return text.replace(" ", "_")


def _research_field_breakdown(truth: dict[str, Any], predictions: list[dict[str, Any]], store: BatchStore, batch_id: str) -> dict[str, Any]:
    fields = ["pricing", "education_discount", "public_api_docs"]
    truth_pairs = {
        (Path(row["source"]).name, _research_field_for_fact(row["fact"]))
        for row in truth["facts"]
    }
    target_by_item = {row["id"]: Path(row["target"]).name for row in store.items(batch_id)}
    pred_pairs = {
        (target_by_item.get(p["work_item_id"], ""), str(p.get("field") or ""))
        for p in predictions
        if p.get("field") and p.get("value") and p.get("evidence")
    }
    per_field = {}
    found = not_found = unsupported = 0
    for field in fields:
        expected = {pair for pair in truth_pairs if pair[1] == field}
        predicted = {pair for pair in pred_pairs if pair[1] == field}
        tp = len(expected & predicted)
        fp = len(predicted - expected)
        fn = len(expected - predicted)
        per_field[field] = {
            "precision": tp / (tp + fp) if tp + fp else 1.0 if not expected else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 1.0,
            "found": len(predicted),
            "false_positive": fp,
            "missed": fn,
        }
        found += len(predicted)
    for row in store.results(batch_id):
        data = json.loads(row["structured_data"])
        for state in (data.get("fields") or {}).values():
            if isinstance(state, dict) and state.get("status") == "not_found":
                not_found += 1
        unsupported += int((data.get("_quality") or {}).get("unsupported_findings_rejected") or 0)
    return {"field_metrics": per_field, "found": found, "not_found": not_found, "unsupported_findings": unsupported}


def _process_rss_mb() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)


def _rss_checkpoints(store: BatchStore, batch_id: str, observed: dict[str, float | None]) -> dict[str, float | None]:
    rss = {}
    for ordinal in (10, 25, 50, 100):
        item = next((row for row in store.items(batch_id) if row["ordinal"] == ordinal), None)
        rss[str(ordinal)] = observed.get(str(ordinal)) if item else None
    return rss


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["assignment", "research"], default="assignment")
    parser.add_argument("--targets", type=int, default=10)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=90.0)
    args = parser.parse_args()
    default_name = f"phase5_live_{args.kind}_{args.targets}"
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "runtime" / "benchmark_runs" / default_name
    await run_live(args.kind, args.targets, output_dir, args.max_steps, args.max_seconds)


if __name__ == "__main__":
    asyncio.run(main())
