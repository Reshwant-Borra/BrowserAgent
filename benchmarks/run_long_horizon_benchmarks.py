from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from run_smoke_benchmarks import ROOT, _run_trial, _start_fixture_server, _summarize


VARIANTS = {
    "A_recent_only": {
        "recent_actions": 5,
        "enable_running_summary": False,
        "enable_memory_retrieval": False,
        "enable_active_facts": False,
    },
    "B_summary": {
        "recent_actions": 5,
        "enable_running_summary": True,
        "enable_memory_retrieval": False,
        "enable_active_facts": False,
    },
    "C_page_aware_retrieval": {
        "recent_actions": 5,
        "enable_running_summary": True,
        "enable_memory_retrieval": True,
        "enable_active_facts": False,
    },
    "D_active_facts": {
        "recent_actions": 5,
        "enable_running_summary": True,
        "enable_memory_retrieval": True,
        "enable_active_facts": True,
    },
    "E_constraint_guard": {
        "recent_actions": 5,
        "enable_running_summary": True,
        "enable_memory_retrieval": True,
        "enable_active_facts": True,
        "enforce_active_fact_constraints": True,
    },
}


def _write_variant_config(output_dir: Path, variant_name: str, overrides: dict[str, Any]) -> Path:
    path = output_dir / f"{variant_name}.yaml"
    data = {
        "context": {
            "max_total_tokens": 4096,
            "recent_window_tokens": 800,
            "summary_tokens": 500,
            "retrieved_memory_tokens": 500,
            "page_tokens": 1400,
            "retrieved_memory_top_k": 5,
            "summary_rebuild_interval": 4,
            **overrides,
        },
        "browser": {"headless": True, "interactive_approval": False},
        "model": {"backend": "ollama", "model_name": "qwen3:8b", "temperature": 0.1},
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return path


def _variant_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _summarize(records)
    prompt_values = [
        r.get("prompt_tokens_avg") for r in records
        if r.get("prompt_tokens_avg") is not None
    ]
    latency_early = [
        r.get("latency_first_half_ms_avg") for r in records
        if r.get("latency_first_half_ms_avg") is not None
    ]
    latency_late = [
        r.get("latency_second_half_ms_avg") for r in records
        if r.get("latency_second_half_ms_avg") is not None
    ]
    summary["aggregate"] = {
        "trials": len(records),
        "passes": sum(1 for r in records if r["pass"]),
        "prompt_tokens_avg": statistics.mean(prompt_values) if prompt_values else None,
        "prompt_tokens_max": max((r.get("prompt_tokens_max") or 0 for r in records), default=None),
        "latency_early_ms_avg": statistics.mean(latency_early) if latency_early else None,
        "latency_late_ms_avg": statistics.mean(latency_late) if latency_late else None,
        "retrieved_memory_count_total": sum(r.get("retrieved_memory_count_total", 0) for r in records),
    }
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-file", default=str(ROOT / "benchmarks" / "long_horizon_tasks.yaml"))
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / ("phase4_" + time.strftime("%Y%m%d_%H%M%S"))))
    parser.add_argument("--variant", choices=[*VARIANTS.keys(), "all"], default="all")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(args.tasks_file, "r", encoding="utf-8") as f:
        tasks = yaml.safe_load(f)["tasks"]

    selected = VARIANTS.items() if args.variant == "all" else [(args.variant, VARIANTS[args.variant])]
    server, base_url = _start_fixture_server()
    try:
        all_summaries = {}
        for variant_name, overrides in selected:
            variant_dir = output_dir / variant_name
            variant_dir.mkdir(parents=True, exist_ok=True)
            config_path = _write_variant_config(variant_dir, variant_name, overrides)
            records = []
            for trial in range(1, args.trials + 1):
                for task in tasks:
                    records.append(await _run_trial(str(config_path), task, base_url, variant_dir, trial))
            summary = _variant_summary(records)
            summary["variant"] = variant_name
            with open(variant_dir / "summary.json", "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            all_summaries[variant_name] = summary["aggregate"]
        with open(output_dir / "phase4_ablation_summary.json", "w", encoding="utf-8") as f:
            json.dump(all_summaries, f, indent=2)
        print(json.dumps(all_summaries, indent=2))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
