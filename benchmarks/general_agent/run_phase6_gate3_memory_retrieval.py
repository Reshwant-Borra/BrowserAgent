"""Phase 6 falsification: Gate 3 (embeddings).

Architecture doc (BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 18,
Phase 6 table): "Build [embeddings] only if FTS5/structured skill retrieval misses
semantically relevant skills/memory often enough to hurt benchmark success. Do not build if
lexical/metadata retrieval remains sufficient."

Procedural skills do not exist yet (architecture doc section 15: LATER, gated on the general
controller already succeeding without them), so "skill retrieval" has no real system to test.
What DOES exist and DOES retrieve today is memory/task_memory.py's FTS5-backed
TaskMemoryStore.search()/search_queries() — the mechanism this gate is actually asking about
in the only form that currently exists in production.

This is a deterministic test (real SQLite FTS5 via the real production TaskMemoryStore code,
no live model/browser needed — retrieval itself is pure keyword search, not an LLM call):
write realistic memory records (the same shapes memory/task_memory.py::derive_memories
actually produces from real event payloads) using vocabulary an agent would plausibly record,
then query with paraphrased/synonymous vocabulary a later step or a different phrasing of the
same goal would plausibly use, and measure whether the real content-relevant record is still
retrieved within the top-k the production context_builder actually uses.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory.event_store import EventStore
from memory.task_memory import TaskMemoryStore

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Each case: memory content written in one vocabulary, later queried in a genuinely different
# (paraphrased/synonymous, not just re-ordered) vocabulary — the exact class of miss embeddings
# would supposedly close and pure keyword overlap would not.
CASES: list[dict[str, str]] = [
    {
        "name": "cheapest_vs_lowest_price",
        "memory": "Observed fact: Item price is $42.00, the lowest of the three listed",
        "query": "which candidate has the lowest cost",
    },
    {
        "name": "due_date_vs_deadline",
        "memory": "Observed fact: Assignment due date is March 3",
        "query": "what is the deadline for this assignment",
    },
    {
        "name": "login_vs_signin",
        "memory": "Blocked: login page detected, manual authentication required",
        "query": "is the user required to sign in",
    },
    {
        "name": "cancel_vs_abort",
        "memory": "Step 4 failed: click target=cancel_button at http://example.test/checkout",
        "query": "did the abort action fail on the checkout page",
    },
    {
        "name": "download_vs_save",
        "memory": "Downloaded artifact report.pdf to ./runtime/downloads/report.pdf",
        "query": "where was the file saved on disk",
    },
    {
        "name": "rating_vs_review_score",
        "memory": "Observed fact: rating = 4.6 out of 5 based on 1200 reviews",
        "query": "what was the review score for this product",
    },
    {
        "name": "stipend_vs_pay",
        "memory": "Observed fact: stipend_usd_per_week = 900",
        "query": "how much does this internship pay per week",
    },
    {
        "name": "unrelated_negative_control",
        "memory": "Observed fact: shipping_cost_usd = 5.00",
        "query": "what color options are available",
    },
]


def run_case(case: dict[str, str]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "task.db"
        store = EventStore(db_path)
        try:
            store.create_task("t1", "test goal", [])
            mem = TaskMemoryStore(store)
            mem.write_memory("t1", "fact", case["memory"], source_event_id=1)
            # A handful of realistic distractor memories every real task also accumulates, so
            # this isn't testing retrieval against an empty index (which would trivially "pass").
            for i, distractor in enumerate([
                "Current subgoal set to: visit the next candidate detail page",
                "Completed extract target=3 at http://example.test/item2",
                "Observed fact: brand = Acme Corp",
            ], start=2):
                mem.write_memory("t1", "fact", distractor, source_event_id=i)

            results = mem.search("t1", case["query"], top_k=5, token_budget=500)
            retrieved_contents = [r.content for r in results]
            found = any(case["memory"] == c for c in retrieved_contents)
            return {
                "name": case["name"],
                "memory_written": case["memory"],
                "query": case["query"],
                "top_k_retrieved": retrieved_contents,
                "target_memory_retrieved": found,
            }
        finally:
            store.close()


def main() -> dict[str, Any]:
    results = [run_case(c) for c in CASES]
    # The negative control is EXPECTED to miss (unrelated content) — it's not counted as a
    # failure of retrieval, it's a sanity check that the harness isn't trivially permissive.
    scored = [r for r in results if r["name"] != "unrelated_negative_control"]
    hits = sum(1 for r in scored if r["target_memory_retrieved"])
    miss_rate = 1 - (hits / len(scored))
    negative_control = next(r for r in results if r["name"] == "unrelated_negative_control")

    report = {
        "cases": results,
        "paraphrase_cases_total": len(scored),
        "paraphrase_cases_retrieved": hits,
        "paraphrase_miss_rate": round(miss_rate, 3),
        "negative_control_correctly_not_retrieved": not negative_control["target_memory_retrieved"],
        "gate3_embeddings": {
            # Doc's own text: "misses ... OFTEN ENOUGH TO HURT benchmark success." No live
            # benchmark has ever shown a task fail because a fact existed but wasn't retrieved
            # (Phase 4B's own finding was the opposite: facts were retrieved but not *applied*,
            # fixed with a deterministic constraint guard, not better retrieval). This case set
            # measures the retrieval mechanism in isolation on its hardest input (genuine
            # vocabulary mismatch, not just reordering).
            "build_condition_met": miss_rate > 0.5,
        },
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"phase6_gate3_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWritten to {out_path}")
    return report


if __name__ == "__main__":
    main()
