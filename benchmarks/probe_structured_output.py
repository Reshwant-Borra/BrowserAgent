from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from agent.config import load_config
from agent.context_builder import build_prompt
from agent.decision import parse_model_output, validate_against_observation
from agent.schemas import DecisionValidationError
from browser.page_model import ElementRef, PageObservation, SelectorHint
from inference.llama_client import create_inference_client
from memory.models import TaskRecord, TaskState

ROOT = Path(__file__).resolve().parent.parent
GRAMMAR_PATH = ROOT / "inference" / "grammar" / "action.gbnf"


def _el(id_: int, role: str, name: str, *, value: str | None = None,
        options: list[str] | None = None, href: str | None = None) -> ElementRef:
    return ElementRef(
        id=id_,
        role=role,
        name=name,
        value=value,
        options=options,
        href=href,
        selector_hint=SelectorHint(css="x", nth=id_),
    )


def _obs(elements: list[ElementRef] | None = None, texts: list[str] | None = None,
         url: str = "http://example.test/page") -> PageObservation:
    return PageObservation(
        url=url,
        title="Probe Page",
        elements=elements or [],
        visible_text=texts or [],
        state_hash="probe",
        element_count=len(elements or []),
    )


CASES: list[dict[str, Any]] = [
    {
        "name": "open_url",
        "goal": "Open http://example.test/docs.",
        "criteria": ["docs"],
        "obs": _obs(url="about:blank"),
        "expected": {"action": "open_url", "url": "http://example.test/docs"},
    },
    {
        "name": "click",
        "goal": "Click the Continue button.",
        "obs": _obs([_el(17, "button", "Continue")]),
        "expected": {"action": "click", "target": 17},
    },
    {
        "name": "type",
        "goal": "Type search term into the Search box.",
        "obs": _obs([_el(4, "textbox", "Search", value="")]),
        "expected": {"action": "type", "target": 4},
    },
    {
        "name": "select",
        "goal": "Choose Compact from the Layout dropdown.",
        "obs": _obs([_el(8, "select", "Layout", value="Comfortable", options=["Comfortable", "Compact"])]),
        "expected": {"action": "select", "target": 8, "value": "Compact"},
    },
    {
        "name": "scroll",
        "goal": "Scroll down to see more content.",
        "obs": _obs(texts=["More content is below the fold."]),
        "expected": {"action": "scroll"},
    },
    {
        "name": "back",
        "goal": "Go back to the previous page.",
        "obs": _obs(url="http://example.test/details"),
        "expected": {"action": "back"},
    },
    {
        "name": "extract",
        "goal": "Extract the visible page text.",
        "obs": _obs(texts=["Account balance is visible."]),
        "expected": {"action": "extract"},
    },
    {
        "name": "download",
        "goal": "Download report.csv.",
        "obs": _obs([_el(31, "link", "Download report.csv", href="/report.csv")]),
        "expected": {"action": "download", "target": 31},
    },
    {
        "name": "wait",
        "goal": "Wait until the page says Ready.",
        "obs": _obs(texts=["Loading"]),
        "expected": {"action": "wait"},
    },
    {
        "name": "finish",
        "goal": "Finish because the requested success state is visible.",
        "criteria": ["Ready"],
        "obs": _obs(texts=["Ready"]),
        "expected": {"action": "finish"},
    },
]


def _task_correct(decision, expected: dict[str, Any]) -> bool:
    if decision.action.value != expected["action"]:
        return False
    if "target" in expected and decision.target != expected["target"]:
        return False
    if "value" in expected and decision.params.get("value") != expected["value"]:
        return False
    if "url" in expected and decision.params.get("url") != expected["url"]:
        return False
    return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", default=str(ROOT / "runtime" / "benchmark_runs" / "phase1b" / "probe_action_specific"))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    client = create_inference_client(config)
    grammar = GRAMMAR_PATH.read_text(encoding="utf-8")

    rows = []
    for repeat in range(1, args.repeats + 1):
        for case in CASES:
            task = TaskRecord(
                id=f"{case['name']}_{repeat}",
                created_at="probe",
                status="running",
                goal=case["goal"],
                success_criteria=case.get("criteria", []),
            )
            prompt = build_prompt(task, TaskState(task_id=task.id), case["obs"], 3000, 12)
            started = time.monotonic()
            completion = await client.complete(prompt, grammar=grammar, max_tokens=config.model.max_output_tokens)
            latency_ms = (time.monotonic() - started) * 1000
            row = {
                "case": case["name"],
                "repeat": repeat,
                "raw": completion.text,
                "syntax_valid": False,
                "schema_valid": False,
                "semantic_valid": False,
                "task_correct": False,
                "latency_ms": latency_ms,
                "prompt_tokens": completion.prompt_tokens,
                "output_tokens": completion.predicted_tokens,
                "prompt_ms": completion.prompt_ms,
                "predicted_ms": completion.predicted_ms,
            }
            try:
                decision = parse_model_output(completion.text)
                row["syntax_valid"] = True
                row["schema_valid"] = True
                validate_against_observation(decision, case["obs"])
                row["semantic_valid"] = True
                row["task_correct"] = _task_correct(decision, case["expected"])
                row["decision"] = decision.model_dump(mode="json")
            except DecisionValidationError as e:
                row["error"] = e.kind.value
                row["message"] = e.message
                if e.kind.value != "malformed_json":
                    row["syntax_valid"] = True
                if e.kind.value not in {"malformed_json", "schema_invalid"}:
                    row["schema_valid"] = True
            rows.append(row)

    summary = {
        "calls": len(rows),
        "syntax_valid_rate": sum(r["syntax_valid"] for r in rows) / len(rows),
        "schema_valid_rate": sum(r["schema_valid"] for r in rows) / len(rows),
        "semantic_valid_rate": sum(r["semantic_valid"] for r in rows) / len(rows),
        "task_correct_rate": sum(r["task_correct"] for r in rows) / len(rows),
        "rows": rows,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
