from __future__ import annotations

from batch.models import ResultContract
from batch.result_quality import normalize_assignment_status, normalize_structured_result


def test_assignment_schema_marks_upcoming_actionable():
    contract = ResultContract(name="assignment")
    structured, quality = normalize_structured_result(
        {
            "relevant": True,
            "summary": "assignment",
            "findings": [{
                "course": "Biology",
                "title": "Unit 4 Lab",
                "due_date": "Due Sep 14",
                "status": "upcoming",
                "actionable": True,
                "evidence": "Assignment due September 14 at 11:59 PM",
            }],
        },
        contract,
        "http://a.test",
        "Biology\nUnit 4 Lab\nAssignment due September 14 at 11:59 PM",
    )
    assert structured["findings"][0]["actionable"] is True
    assert structured["findings"][0]["status"] == "upcoming"
    assert quality["schema_valid_results"] == 1


def test_completed_assignment_conflict_is_not_actionable():
    contract = ResultContract(name="assignment")
    structured, quality = normalize_structured_result(
        {
            "relevant": True,
            "summary": "assignment",
            "findings": [{
                "course": "History",
                "title": "Old Review Packet",
                "due_date": "Deadline: September 18, 2026",
                "status": "upcoming",
                "actionable": True,
                "evidence": "Deadline: September 18, 2026",
            }],
        },
        contract,
        "http://a.test",
        "History\nOld Review Packet\nDeadline: September 18, 2026\nCompleted assignment from last month",
    )
    assert structured["findings"][0]["actionable"] is False
    assert structured["findings"][0]["status"] in {"completed", "past_archived"}
    assert quality["status_conflicts"] == 1


def test_unknown_assignment_status_is_not_actionable():
    assert normalize_assignment_status("maybe", "Ambiguous page", "") == "unknown"
    structured, _ = normalize_structured_result(
        {
            "relevant": True,
            "findings": [{
                "course": "English",
                "title": "Essay Draft",
                "status": "maybe",
                "actionable": True,
                "evidence": "Essay Draft",
            }],
        },
        ResultContract(name="assignment"),
        "http://a.test",
        "Essay Draft",
    )
    assert structured["findings"][0]["status"] == "unknown"
    assert structured["findings"][0]["actionable"] is False


def test_research_fields_support_found_not_found_and_unresolved():
    contract = ResultContract(
        name="research",
        required_fields=["pricing", "education_discount", "public_api_docs"],
    )
    structured, quality = normalize_structured_result(
        {
            "relevant": True,
            "fields": {
                "pricing": {
                    "status": "found",
                    "value": "$12 per seat",
                    "evidence": "Pricing starts at $12 per seat.",
                    "source_url": "http://a.test",
                },
                "education_discount": {"status": "not_found"},
                "public_api_docs": {"status": "found", "value": "available"},
            },
            "findings": [{"field": "source", "value": "Relevant source", "evidence": "Relevant source"}],
        },
        contract,
        "http://a.test",
        "Acme Analytics\nRelevant source\nPricing starts at $12 per seat.",
    )
    assert structured["fields"]["pricing"]["status"] == "found"
    assert structured["fields"]["education_discount"]["status"] == "not_found"
    assert structured["fields"]["public_api_docs"]["status"] == "unresolved"
    assert structured["findings"] == [{
        "field": "pricing",
        "value": "$12 per seat",
        "evidence": "Pricing starts at $12 per seat.",
        "source_url": "http://a.test",
    }]
    assert quality["unsupported_findings_rejected"] == 2


def test_research_legacy_finding_maps_to_requested_field():
    contract = ResultContract(
        name="research",
        required_fields=["pricing", "education_discount", "public_api_docs"],
    )
    structured, quality = normalize_structured_result(
        {
            "relevant": True,
            "findings": [{
                "type": "documentation",
                "value": "available",
                "evidence": "Public REST API documentation is available.",
            }],
        },
        contract,
        "http://a.test",
        "Vector Loom\nPublic REST API documentation is available.",
    )
    assert structured["fields"]["public_api_docs"]["status"] == "found"
    assert structured["findings"][0]["field"] == "public_api_docs"
    assert quality["evidence_backed_findings"] == 1


def test_invalid_result_schema_is_rejected():
    structured, quality = normalize_structured_result(
        {"relevant": "yes", "findings": "not-a-list"},
        ResultContract(name="generic"),
        "http://a.test",
        "Evidence",
    )
    assert structured["findings"] == []
    assert quality["unsupported_findings_rejected"] == 0
