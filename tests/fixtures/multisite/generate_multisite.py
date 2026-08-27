from __future__ import annotations

import json
from pathlib import Path


ASSIGNMENT_VARIANTS = [
    ("Biology", "Unit 4 Lab", "Due Sep 14", "upcoming", "Assignment due September 14 at 11:59 PM"),
    ("History", "Primary Source Notes", "Deadline: September 18, 2026", "upcoming", "Deadline: September 18, 2026"),
    ("Algebra", "Quadratics Practice", "Submit by 11:59 PM Friday", "upcoming", "Submit by 11:59 PM Friday"),
    ("English", "Essay Draft", "", "missing_due_date", "Essay Draft assigned; due date to be announced"),
    ("Chemistry", "Safety Quiz", "Assignment closed", "closed", "Assignment closed last week"),
]

RESEARCH_VARIANTS = [
    ("Acme Analytics", True, "publishes pricing by seat", "Pricing starts at $12 per seat."),
    ("Northstar Labs", True, "offers education discounts", "Education discount available for verified schools."),
    ("Blue Harbor", False, "", "Company news and careers only."),
    ("Vector Loom", True, "has public API docs", "Public REST API documentation is available."),
    ("Cedar Works", False, "", "No relevant research facts found."),
]


def generate_assignment_fixture(root: Path, count: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    truth = {"targets": [], "assignments": []}
    duplicate_written = False
    for i in range(1, count + 1):
        course, title, due, status, evidence = ASSIGNMENT_VARIANTS[(i - 1) % len(ASSIGNMENT_VARIANTS)]
        target = f"assignment_{i:03d}.html"
        actionable = status in {"upcoming", "missing_due_date"}
        if i % 7 == 0:
            title = "No assignments"
            evidence = "No assignments due"
            actionable = False
            status = "none"
        if i % 11 == 0:
            title = "Old Review Packet"
            evidence = "Completed assignment from last month"
            actionable = False
            status = "completed"
        if i in {13, 37}:
            course = "Biology"
            title = "Unit 4 Lab"
            due = "Due Sep 14"
            evidence = "Assignment due September 14 at 11:59 PM"
            actionable = True
            status = "upcoming"
            duplicate_written = True
        html = _assignment_page(course, title, due, status, evidence, i)
        (root / target).write_text(html, encoding="utf-8")
        url = target
        truth["targets"].append(url)
        if actionable:
            truth["assignments"].append({
                "course": course,
                "assignment": title,
                "due_date": due,
                "status": status,
                "source": url,
                "evidence": evidence,
            })
    truth["contains_duplicate_assignment"] = duplicate_written
    (root / "assignment_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return truth


def generate_research_fixture(root: Path, count: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    truth = {"targets": [], "facts": []}
    for i in range(1, count + 1):
        name, relevant, fact, evidence = RESEARCH_VARIANTS[(i - 1) % len(RESEARCH_VARIANTS)]
        target = f"research_{i:03d}.html"
        (root / target).write_text(_research_page(name, relevant, fact, evidence, i), encoding="utf-8")
        truth["targets"].append(target)
        if relevant:
            truth["facts"].append({"source": target, "relevant": True, "fact": fact, "evidence": evidence})
    (root / "research_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    return truth


def _assignment_page(course: str, title: str, due: str, status: str, evidence: str, i: int) -> str:
    due_html = f"<p class='due'>{due}</p>" if due else "<p class='due missing'>Due date to be announced</p>"
    next_link = f"<a href='assignment_{i + 1:03d}.html'>Next</a>" if i % 5 == 0 else ""
    return f"""<!doctype html>
<html><head><title>{course} Portal</title></head>
<body>
<nav>Assignments Grades Syllabus Announcements</nav>
<main>
<h1>{course}</h1>
<section class="announcement">Reminder: bring notebook to class.</section>
<article data-status="{status}">
<h2>{title}</h2>
{due_html}
<p>{evidence}</p>
</article>
<aside>Old assignments and grade summaries may appear here.</aside>
{next_link}
</main>
</body></html>"""


def _research_page(name: str, relevant: bool, fact: str, evidence: str, i: int) -> str:
    marker = "Relevant source" if relevant else "General company page"
    return f"""<!doctype html>
<html><head><title>{name}</title></head>
<body>
<header><h1>{name}</h1><p>{marker}</p></header>
<main>
<p>{evidence}</p>
<section>Careers, press, leadership, and product overview.</section>
<a href="research_{max(1, i - 1):03d}.html">Related</a>
<script>window.fixtureLoaded = true;</script>
</main>
</body></html>"""

