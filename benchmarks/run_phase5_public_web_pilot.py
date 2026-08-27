from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from batch.models import BatchPolicy, NavigationScope, ResultContract
from batch.orchestrator import BatchOrchestrator
from batch.store import BatchStore


TARGETS = [
    "https://example.com/",
    "https://www.iana.org/help/example-domains",
    "https://www.python.org/",
    "https://docs.python.org/3/",
    "https://www.sqlite.org/index.html",
    "https://www.w3.org/",
    "https://www.rfc-editor.org/",
    "https://www.loc.gov/",
    "https://www.nih.gov/",
    "https://www.noaa.gov/",
]


def _contract() -> ResultContract:
    return ResultContract(
        name="generic",
        description=(
            "Classify whether the public page loaded and extract one concise, evidence-backed "
            "fact about the page purpose, organization, or primary resource."
        ),
        required_fields=["page_topic", "organization_or_resource", "source_url", "evidence"],
        field_definitions={
            "page_topic": "the main subject or purpose of the page",
            "organization_or_resource": "the organization, documentation set, or public resource represented by the page",
        },
    )


async def run(output_dir: Path, max_steps: int, max_seconds: float) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(None)
    config.browser.headless = True
    config.browser.interactive_approval = False
    config.storage.runtime_dir = str(output_dir / "runtime")
    config.storage.tasks_dir = str(output_dir / "runtime" / "tasks")
    config.browser.user_data_dir = str(output_dir / "runtime" / "tasks")
    config.logging.dir = str(output_dir / "runtime" / "logs")
    config.model.request_timeout_s = max(float(config.model.request_timeout_s), max_seconds)
    policy = BatchPolicy(
        continue_on_failure=True,
        work_item_max_attempts=1,
        max_steps_per_item=max_steps,
        max_seconds_per_item=max_seconds,
        read_only=True,
        navigation_scope=NavigationScope.SAME_ORIGIN,
    )
    contract = _contract()
    batch_id = "public_web_pilot"
    store = BatchStore.for_batch_dir(output_dir / "runtime" / "batches" / batch_id)
    try:
        store.create_batch(
            "Phase 5 public-web pilot: load benign public pages and extract one evidence-backed page-purpose fact",
            TARGETS,
            contract,
            policy,
            batch_id=batch_id,
        )
        started = time.monotonic()
        final = await BatchOrchestrator(config, store, batch_id, policy, contract).run()
        duration = time.monotonic() - started
        rows = []
        for item in store.items(batch_id):
            row = dict(item)
            result = next((dict(r) for r in store.results(batch_id) if r["work_item_id"] == row["id"]), None)
            classification = _classification(row, result)
            rows.append({
                "ordinal": row["ordinal"],
                "target": row["target"],
                "status": row["status"],
                "classification": classification["classification"],
                "root_category": classification["root_category"],
                "failure_category": row.get("failure_category"),
                "summary": result["summary"] if result else "",
                "finding_count": len(json.loads(result["structured_data"]).get("findings", [])) if result else 0,
                "evidence": json.loads(result["evidence"])[:2] if result else [],
            })
        metric_rows = _metric_rows(config, store, batch_id)
        summary = {
            "duration_s": duration,
            "batch_status": final["status"],
            "targets": len(TARGETS),
            "completed": sum(1 for row in rows if row["classification"] == "completed"),
            "irrelevant": sum(1 for row in rows if row["classification"] == "irrelevant"),
            "failed": sum(1 for row in rows if row["classification"] == "failed"),
            "blocked": sum(1 for row in rows if row["classification"] == "blocked"),
            "queue": {
                "completed": final["completed"],
                "failed": final["failed"],
                "blocked": final["blocked"],
                "raw_findings": final["raw_findings"],
                "deduplicated_findings": final["deduplicated_findings"],
            },
            "model_calls_total": sum(row["model_calls"] for row in metric_rows),
            "model_calls_per_item_avg": statistics.mean([row["model_calls"] for row in metric_rows]) if metric_rows else 0,
            "rows": rows,
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        store.close()


def _classification(item: dict[str, Any], result: dict[str, Any] | None) -> dict[str, str]:
    if item["status"] == "blocked":
        return {"classification": "blocked", "root_category": item.get("failure_category") or "BLOCKED"}
    if item["status"] == "failed_final":
        return {"classification": "failed", "root_category": item.get("failure_category") or "FAILED"}
    if result is None:
        return {"classification": "failed", "root_category": "NO_RESULT"}
    data = json.loads(result["structured_data"])
    findings = data.get("findings") or []
    if item["status"] == "completed" and findings:
        return {"classification": "completed", "root_category": "OK"}
    if item["status"] == "completed":
        return {"classification": "irrelevant", "root_category": "NO_EVIDENCE_FINDING"}
    return {"classification": "failed", "root_category": item.get("failure_category") or "UNKNOWN"}


def _metric_rows(config, store: BatchStore, batch_id: str) -> list[dict[str, Any]]:
    rows = []
    for item in store.items(batch_id):
        task_id = item["browser_task_id"]
        if not task_id:
            continue
        metrics_path = Path(config.logging.dir) / f"{task_id}.metrics.jsonl"
        metrics = []
        if metrics_path.exists():
            metrics = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows.append({
            "work_item_id": item["id"],
            "model_calls": sum(1 for row in metrics if row.get("event") == "model_call"),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase5_public_web_pilot"))
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=90.0)
    args = parser.parse_args()
    asyncio.run(run(Path(args.output_dir), args.max_steps, args.max_seconds))


if __name__ == "__main__":
    main()
