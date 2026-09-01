"""Resumable supervisor for the Phase 3 live benchmark (run_phase3_entities.py).

Why this exists: a full 5-domain x N-trial live matrix run as one blocking process takes
10-15+ minutes; run as a single foreground invocation it is vulnerable to being killed by
whatever is driving it (shell/tool timeout, terminal close, etc.) before any result is
persisted, losing all completed trial evidence. This supervisor makes the matrix resumable:

- Each (domain, trial) pair runs as its OWN subprocess (`run_phase3_entities.py --domains X`),
  isolated from every other trial — a crash (Playwright/Chrome, Ollama connection, Python
  process exit) in one trial cannot corrupt or lose any other trial's already-persisted result.
- Every completed trial is appended to `<output-dir>/progress.jsonl` immediately, one line per
  trial, flushed and fsynced before moving on.
- On restart, already-completed (domain, trial) pairs (per progress.jsonl) are skipped — only
  missing trials run. `--only-missing` makes this explicit; it is also the default behavior.
- Same model/config/fixture conditions every trial: each subprocess is the unmodified
  run_phase3_entities.py, invoked identically (same config loader, same fixture server
  lifecycle per trial, same continuous-strategy controller).
- A per-trial subprocess timeout classifies a hang as BENCHMARK_INFRA rather than hanging the
  whole matrix forever.

No BrowserAgent architecture is touched by this file — it only orchestrates the existing,
unmodified run_phase3_entities.py as a subprocess and records what happened.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = Path(__file__).resolve().parent / "run_phase3_entities.py"

DOMAIN_NAMES = ["vacuums", "laptops", "hotels", "internships", "papers_assignments"]


def _load_progress(progress_file: Path) -> dict[tuple[str, int], dict[str, Any]]:
    done: dict[tuple[str, int], dict[str, Any]] = {}
    if not progress_file.exists():
        return done
    with progress_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn last line from a prior kill mid-write
            key = (record["scenario"], record["trial"])
            done[key] = record  # last write for a key wins (a rerun overwrites in-memory view)
    return done


def _append_progress(progress_file: Path, record: dict[str, Any]) -> None:
    import os
    with progress_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _classify_subprocess_failure(returncode: int, timed_out: bool, log_tail: str) -> str:
    if timed_out:
        return "BENCHMARK_INFRA"
    low = log_tail.lower()
    if "connectionerror" in low or "connection refused" in low or "ollama" in low and "error" in low:
        return "MODEL_RELIABILITY"
    if "playwright" in low and ("closed" in low or "crash" in low or "target closed" in low):
        return "OBSERVATION"
    if "memoryerror" in low or "out of memory" in low or "oom" in low:
        return "BENCHMARK_INFRA"
    if returncode < 0:
        # negative returncode on POSIX means killed by signal; on Windows subprocess reports
        # the raw (often large/negative-looking) exit code of a terminated process either way.
        return "BENCHMARK_INFRA"
    return "OTHER"


def run_trial(
    domain: str, trial: int, output_dir: Path, timeout_s: float,
) -> dict[str, Any]:
    trial_dir = output_dir / f"{domain}_t{trial}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    log_path = trial_dir / "run.log"
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--domains", domain, "--output-dir", str(trial_dir)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout_s,
        )
        returncode = proc.returncode
        log_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = -1
        log_text = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + \
                    ((exc.stderr or "") if isinstance(exc.stderr, str) else "")
    duration = time.monotonic() - start
    log_path.write_text(log_text, encoding="utf-8")

    result_file = trial_dir / "phase3_entities_results.json"
    domain_result: Optional[dict[str, Any]] = None
    if result_file.exists():
        try:
            summary = json.loads(result_file.read_text(encoding="utf-8"))
            results = summary.get("results") or []
            domain_result = results[0] if results else None
        except (json.JSONDecodeError, IndexError, KeyError):
            domain_result = None

    if domain_result is not None:
        record = {
            "scenario": domain, "trial": trial,
            "status": domain_result.get("status"),
            "success": bool(domain_result.get("generality_gate_pass")),
            "candidate_count": domain_result.get("entities_collected"),
            "model_calls": domain_result.get("model_calls"),
            "actions": domain_result.get("actions"),
            "duration": round(duration, 2),
            "failure_category": domain_result.get("failure_category"),
            "subprocess_returncode": returncode, "subprocess_timed_out": timed_out,
            "detail": domain_result,
        }
    else:
        record = {
            "scenario": domain, "trial": trial,
            "status": "crashed" if not timed_out else "timed_out",
            "success": False,
            "candidate_count": None, "model_calls": None, "actions": None,
            "duration": round(duration, 2),
            "failure_category": _classify_subprocess_failure(returncode, timed_out, log_text),
            "subprocess_returncode": returncode, "subprocess_timed_out": timed_out,
            "detail": {"log_tail": log_text[-2000:]},
        }
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase3_resumable"))
    parser.add_argument("--domains", default=",".join(DOMAIN_NAMES), help="comma-separated domain names")
    parser.add_argument("--trials", type=int, default=2, help="trials per domain")
    parser.add_argument("--trial-timeout-s", type=float, default=240.0)
    parser.add_argument("--only-missing", action="store_true", default=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_file = output_dir / "progress.jsonl"
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]

    done = _load_progress(progress_file)
    plan = [(d, t) for d in domains for t in range(1, args.trials + 1)]
    remaining = [(d, t) for (d, t) in plan if (d, t) not in done]

    print(f"[resumable] {len(done)} trial(s) already recorded, {len(remaining)} remaining "
          f"of {len(plan)} planned", file=sys.stderr)

    for domain, trial in remaining:
        print(f"--- {domain} trial {trial}/{args.trials} ---", file=sys.stderr)
        record = run_trial(domain, trial, output_dir, args.trial_timeout_s)
        _append_progress(progress_file, record)
        print(json.dumps({k: v for k, v in record.items() if k != "detail"}, indent=2), file=sys.stderr)

    # Recompute the full summary from progress.jsonl every time (not just this invocation's
    # remaining set), so a resumed run's report always reflects ALL trials ever recorded.
    all_done = _load_progress(progress_file)
    per_domain: dict[str, list[dict[str, Any]]] = {d: [] for d in domains}
    for (d, _t), rec in sorted(all_done.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if d in per_domain:
            per_domain[d].append(rec)
    domains_solved_at_least_once = [d for d in domains if any(r["success"] for r in per_domain[d])]
    summary = {
        "domains_planned": domains,
        "trials_per_domain": args.trials,
        "trials_recorded": len(all_done),
        "trials_planned": len(plan),
        "domains_solved_at_least_once": domains_solved_at_least_once,
        "domains_solved_count": len(domains_solved_at_least_once),
        "generality_gate_met": len(domains_solved_at_least_once) >= 4,
        "per_domain": per_domain,
    }
    (output_dir / "phase3_resumable_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_domain"}, indent=2))


if __name__ == "__main__":
    main()
