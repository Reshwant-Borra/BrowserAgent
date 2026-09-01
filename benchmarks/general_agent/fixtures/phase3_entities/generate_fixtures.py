"""Generates the Phase 3 generality-gate holdout fixtures (BrowserAgent_General_Autonomous_
Agent_Architecture_REVISED.pdf, section 18 Phase 3: "Benchmark holdouts: vacuum fixture ->
laptops -> hotels -> internships -> papers/assignments with no production-code changes").

Five domains, same directory-page-links-to-independent-detail-pages ("hub and branch") shape
already proven reliable for the continuous controller strategy (compare_and_report, Section 32
of docs/BROWSERAGENT_MASTER_STATUS.md: 5/5 live). Each domain's items carry two attributes so a
"cheapest"/"best-rated"/"highest-paying"/"most urgent" objective is meaningful; the actual
selection is always done by agent/ranking.py's live semantic ranking call, never a per-domain
code path here — this script only emits static HTML.

Re-run with `python benchmarks/general_agent/fixtures/phase3_entities/generate_fixtures.py`
after editing DOMAINS; output is deterministic and checked in, so this does not need to run as
part of any test or benchmark invocation.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

DOMAINS: dict[str, dict] = {
    "vacuums": {
        "directory_title": "Vacuum Cleaners",
        "item_noun": "vacuum cleaner",
        "attr_labels": {"price_usd": "Price", "rating": "Rating"},
        "attr_fmt": {"price_usd": "${}", "rating": "{}/5"},
        "items": [
            ("AeroClean 200", {"price_usd": "89.99", "rating": "3.9"}),
            ("DustHunter Pro", {"price_usd": "142.50", "rating": "4.4"}),
            ("QuietSweep Mini", {"price_usd": "59.00", "rating": "4.1"}),
        ],
    },
    "laptops": {
        "directory_title": "Laptops",
        "item_noun": "laptop",
        "attr_labels": {"price_usd": "Price", "rating": "Rating"},
        "attr_fmt": {"price_usd": "${}", "rating": "{}/5"},
        "items": [
            ("SwiftBook Air", {"price_usd": "899.00", "rating": "4.2"}),
            ("ForgeLine Pro 15", {"price_usd": "1499.00", "rating": "4.6"}),
            ("ValueNote 3", {"price_usd": "549.00", "rating": "3.8"}),
        ],
    },
    "hotels": {
        "directory_title": "Hotels Near Downtown",
        "item_noun": "hotel",
        "attr_labels": {"price_per_night_usd": "Price per night", "rating": "Guest rating"},
        "attr_fmt": {"price_per_night_usd": "${}", "rating": "{}/5"},
        "items": [
            ("Harbor View Inn", {"price_per_night_usd": "129.00", "rating": "4.1"}),
            ("Cedar Plaza Hotel", {"price_per_night_usd": "219.00", "rating": "4.6"}),
            ("Budget Stay Downtown", {"price_per_night_usd": "79.00", "rating": "3.6"}),
        ],
    },
    "internships": {
        "directory_title": "Summer Internships",
        "item_noun": "internship listing",
        "attr_labels": {"stipend_usd_per_week": "Weekly stipend", "duration_weeks": "Duration"},
        "attr_fmt": {"stipend_usd_per_week": "${}", "duration_weeks": "{} weeks"},
        "items": [
            ("DataForge Analytics Intern", {"stipend_usd_per_week": "850", "duration_weeks": "10"}),
            ("Greenfield Labs Research Intern", {"stipend_usd_per_week": "1200", "duration_weeks": "12"}),
            ("Marketing at BrightPath", {"stipend_usd_per_week": "600", "duration_weeks": "8"}),
        ],
    },
    "papers_assignments": {
        "directory_title": "Course Reading List",
        "item_noun": "reading",
        "attr_labels": {"due_in_days": "Due in", "points": "Points"},
        "attr_fmt": {"due_in_days": "{} days", "points": "{} pts"},
        "items": [
            ("Problem Set 4: Graph Algorithms", {"due_in_days": "2", "points": "40"}),
            ("Essay: Comparative Policy Analysis", {"due_in_days": "9", "points": "60"}),
            ("Lab Report: Titration Accuracy", {"due_in_days": "1", "points": "25"}),
        ],
    },
}


def _slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")


def _detail_page(title: str, item_name: str, attrs: dict[str, str], attr_labels: dict[str, str],
                  attr_fmt: dict[str, str]) -> str:
    rows = "\n".join(
        f"    <li>{attr_labels[k]}: {attr_fmt[k].format(v)}</li>" for k, v in attrs.items()
    )
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{item_name} - {title}</title></head>
<body>
  <h1>{item_name}</h1>
  <ul>
{rows}
  </ul>
  <p><a href="index.html">Back to {title}</a></p>
</body>
</html>
"""


def _index_page(title: str, item_noun: str, items: list[tuple[str, dict]]) -> str:
    links = "\n".join(
        f'    <li><a href="{_slug(name)}.html">{name}</a></li>' for name, _ in items
    )
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
  <h1>{title}</h1>
  <p>{len(items)} {item_noun}s listed below.</p>
  <ul>
{links}
  </ul>
</body>
</html>
"""


def main() -> None:
    for domain, spec in DOMAINS.items():
        domain_dir = ROOT / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        (domain_dir / "index.html").write_text(
            _index_page(spec["directory_title"], spec["item_noun"], spec["items"]), encoding="utf-8",
        )
        for name, attrs in spec["items"]:
            (domain_dir / f"{_slug(name)}.html").write_text(
                _detail_page(spec["directory_title"], name, attrs, spec["attr_labels"], spec["attr_fmt"]),
                encoding="utf-8",
            )
        print(f"wrote {domain}: index.html + {len(spec['items'])} detail pages")


if __name__ == "__main__":
    main()
