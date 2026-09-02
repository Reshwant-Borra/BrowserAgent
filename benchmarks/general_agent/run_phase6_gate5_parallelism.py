"""Phase 6 falsification: Gate 5 (parallel agents).

Architecture doc (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18,
Phase 6 table): "Build [parallel agents] only if batch throughput is inadequate and
model/browser contention tests show real speedup. Do not build if single local model becomes
the bottleneck or site stability declines."

Phase 4's own scale benchmark (docs/BROWSERAGENT_MASTER_STATUS.md Section 36.3) already showed
25/50/100-target control-plane delegation completing in 2-10s with a FakeBatchChildRunner —
that measured the CONTROLLER's own resolution/ingestion overhead, deliberately isolated from
live-model item latency (that script's own stated scope). This script measures the thing that
was deliberately left unmeasured: whether the shared local Ollama/Qwen3-8B GPU process is
actually the bottleneck during REAL sequential batch-style item processing (real Playwright,
real Qwen3-8B, no scripting) — which is the only case where parallel browser workers could
plausibly help, since a single local GPU-resident model necessarily serializes inference calls
regardless of how many browsers run at once.

Method: run N independent real single-page read tasks sequentially (the same shape a
BatchOrchestrator work item's own child AgentLoop performs), sampling GPU utilization via
`nvidia-smi` on a fixed interval throughout. If the GPU sits near-saturated for most of the
wall-clock time, the shared model is the bottleneck and parallel browser workers would only
contend for the same GPU queue rather than yielding real speedup. If the GPU sits mostly idle,
wall-clock time is dominated by something parallelizable (page loads, browser I/O).
"""
from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import sys
import threading
import time
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.config import load_config
from agent.loop import AgentLoop

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SIMPLE_SITE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "simple_site"

TARGET_PAGES = [
    "candidate_alpha.html", "candidate_beta.html", "candidate_gamma.html",
    "workflow_site_a.html", "workflow_site_b.html", "workflow_site_c.html",
]


def _serve(directory: Path):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


class GpuSampler:
    """Polls nvidia-smi on a fixed interval in a background thread. Independent of and
    unrelated to any BrowserAgent production code — pure external measurement."""

    def __init__(self, interval_s: float = 0.5):
        self.interval_s = interval_s
        self.samples: list[dict[str, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._available = self._probe()

    def _probe(self) -> bool:
        try:
            subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            return True
        except Exception:
            return False

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5, check=True,
                ).stdout.strip()
                util_str, mem_str = out.split(",")
                self.samples.append({
                    "t": t0, "gpu_util_pct": float(util_str.strip()),
                    "gpu_mem_mib": float(mem_str.strip()),
                })
            except Exception:
                pass
            time.sleep(max(0.0, self.interval_s - (time.monotonic() - t0)))

    def start(self) -> None:
        if not self._available:
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


async def _run_read_task(base_url: str, page: str, index: int) -> dict[str, Any]:
    config = load_config(None)
    config.browser.headless = True
    config.browser.interactive_approval = False
    goal = f"Extract the main heading and any price or rating shown on this page, then finish."
    loop = AgentLoop.create_new(config, goal, [], explicit_target_url=f"{base_url}/{page}")
    await loop.start_browser()
    t0 = time.monotonic()
    try:
        await loop.browser.open_url(f"{base_url}/{page}")
        state = await loop.run_steps(4)
        return {
            "index": index, "page": page, "status": state.status,
            "steps": state.current_step, "elapsed_s": round(time.monotonic() - t0, 2),
        }
    finally:
        await loop.aclose()


async def main() -> dict[str, Any]:
    server, base_url = _serve(SIMPLE_SITE_DIR)
    sampler = GpuSampler(interval_s=0.5)
    try:
        sampler.start()
        overall_start = time.monotonic()
        results = []
        for i, page in enumerate(TARGET_PAGES):
            results.append(await _run_read_task(base_url, page, i))
        overall_elapsed = time.monotonic() - overall_start
    finally:
        sampler.stop()
        server.shutdown()
        server.server_close()

    samples = sampler.samples
    if samples:
        avg_util = sum(s["gpu_util_pct"] for s in samples) / len(samples)
        idle_samples = sum(1 for s in samples if s["gpu_util_pct"] < 5.0)
        idle_fraction = idle_samples / len(samples)
        saturated_samples = sum(1 for s in samples if s["gpu_util_pct"] >= 80.0)
        saturated_fraction = saturated_samples / len(samples)
        peak_mem_mib = max(s["gpu_mem_mib"] for s in samples)
    else:
        avg_util = idle_fraction = saturated_fraction = peak_mem_mib = None

    report = {
        "gpu_sampling_available": sampler._available,
        "sample_count": len(samples),
        "items_run": len(TARGET_PAGES),
        "overall_elapsed_s": round(overall_elapsed, 2),
        "per_item_elapsed_s": [r["elapsed_s"] for r in results],
        "per_item_results": results,
        "avg_gpu_util_pct": round(avg_util, 1) if avg_util is not None else None,
        "gpu_idle_fraction": round(idle_fraction, 3) if idle_fraction is not None else None,
        "gpu_saturated_fraction": round(saturated_fraction, 3) if saturated_fraction is not None else None,
        "peak_gpu_mem_mib": peak_mem_mib,
        "gate5_parallel_agents": {
            # Doc's own text: "Build only if batch throughput is inadequate AND model/browser
            # contention tests show real speedup." Phase 4's own control-plane benchmark already
            # showed 100 items resolved/ingested in ~10s (excluding live-model item latency,
            # that script's stated scope) — so "batch throughput is inadequate" has no existing
            # support. This adds the missing half: whether the shared GPU model is itself the
            # bottleneck (high idle_fraction would suggest room for a parallel win; a
            # consistently high/saturated fraction means the one shared local model would just
            # serialize concurrent workers regardless of browser parallelism).
            "build_condition_met": (
                idle_fraction is not None and idle_fraction > 0.5
                and overall_elapsed / max(len(TARGET_PAGES), 1) > 15.0
            ),
        },
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"phase6_gate5_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWritten to {out_path}")
    return report


if __name__ == "__main__":
    asyncio.run(main())
