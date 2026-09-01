"""Deterministic unit tests for agent/workspace_ops.py (Phase 3: generic entity
collection/comparison, BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf,
section 9.1). Every function here is pure and entity-generic — no fixture/domain names appear
in agent/workspace_ops.py itself, and these tests deliberately use made-up field names to
prove that.
"""
from __future__ import annotations

from agent import workspace_ops
from agent.workspace_models import WorkspaceEntity


def _entity(id_, name=None, **attrs) -> WorkspaceEntity:
    return WorkspaceEntity(id=id_, entity_type="candidate", name=name, attributes=attrs)


def test_filter_entities_numeric_ops():
    entities = [_entity("a", price=10), _entity("b", price=20), _entity("c", price=30)]
    assert [e.id for e in workspace_ops.filter_entities(entities, "price", "lt", 25)] == ["a", "b"]
    assert [e.id for e in workspace_ops.filter_entities(entities, "price", "gte", 20)] == ["b", "c"]


def test_filter_entities_string_ops_and_missing_field_dropped():
    entities = [_entity("a", tag="fast"), _entity("b", tag="slow"), _entity("c")]
    assert [e.id for e in workspace_ops.filter_entities(entities, "tag", "contains", "fas")] == ["a"]
    assert [e.id for e in workspace_ops.filter_entities(entities, "tag", "eq", "slow")] == ["b"]


def test_sort_entities_missing_values_sort_last_stably():
    entities = [_entity("a", score=5), _entity("b"), _entity("c", score=1), _entity("d")]
    ordered = workspace_ops.sort_entities(entities, "score", "asc")
    assert [e.id for e in ordered] == ["c", "a", "b", "d"]


def test_sort_entities_desc():
    entities = [_entity("a", score=5), _entity("b", score=9), _entity("c", score=1)]
    ordered = workspace_ops.sort_entities(entities, "score", "desc")
    assert [e.id for e in ordered] == ["b", "a", "c"]


def test_numeric_min_max():
    entities = [_entity("a", price=10), _entity("b", price=2), _entity("c")]
    assert workspace_ops.numeric_min(entities, "price").id == "b"
    assert workspace_ops.numeric_max(entities, "price").id == "a"
    assert workspace_ops.numeric_min([_entity("z")], "price") is None


def test_numeric_parses_currency_strings():
    entities = [_entity("a", price="$19.99"), _entity("b", price="$8.50")]
    assert workspace_ops.numeric_min(entities, "price").id == "b"


def test_date_min_max():
    entities = [
        _entity("a", deadline="2026-03-01"),
        _entity("b", deadline="2026-01-15"),
        _entity("c", deadline="not a date"),
    ]
    assert workspace_ops.date_min(entities, "deadline").id == "b"
    assert workspace_ops.date_max(entities, "deadline").id == "a"


def test_dedupe_entities_first_wins():
    entities = [_entity("a", key="x"), _entity("b", key="x"), _entity("c", key="y")]
    deduped = workspace_ops.dedupe_entities(entities, ["key"])
    assert [e.id for e in deduped] == ["a", "c"]


def test_count_and_group_by():
    entities = [_entity("a", cat="x"), _entity("b", cat="y"), _entity("c", cat="x")]
    assert workspace_ops.count(entities) == 3
    groups = workspace_ops.group_by(entities, "cat")
    assert {k: [e.id for e in v] for k, v in groups.items()} == {"x": ["a", "c"], "y": ["b"]}


def test_select_top_k_with_field():
    entities = [_entity("a", price=30), _entity("b", price=10), _entity("c", price=20)]
    top2 = workspace_ops.select_top_k(entities, 2, field="price", direction="asc")
    assert [e.id for e in top2] == ["b", "c"]


def test_select_top_k_without_field_preserves_order():
    entities = [_entity("a"), _entity("b"), _entity("c")]
    assert [e.id for e in workspace_ops.select_top_k(entities, 2)] == ["a", "b"]


def test_entity_patch_from_findings_none_on_empty():
    assert workspace_ops.entity_patch_from_findings(
        None, entity_id="ent_1", subgoal="s", source_event_id=1, source_url=None,
    ) is None
    assert workspace_ops.entity_patch_from_findings(
        {"findings": []}, entity_id="ent_1", subgoal="s", source_event_id=1, source_url=None,
    ) is None
    assert workspace_ops.entity_patch_from_findings(
        {"findings": [{"field": "price", "value": ""}]},
        entity_id="ent_1", subgoal="s", source_event_id=1, source_url=None,
    ) is None


def test_entity_patch_from_findings_builds_entity_and_evidence():
    structured = {
        "summary": "widget page",
        "findings": [
            {"field": "item_name", "value": "Widget A", "evidence": "title says Widget A"},
            {"field": "price_usd", "value": "19.99", "evidence": "$19.99", "source_url": "http://x/a"},
        ],
    }
    patch = workspace_ops.entity_patch_from_findings(
        structured, entity_id="ent_abc", subgoal="record widget A", source_event_id=42,
        source_url="http://x/a",
    )
    assert patch is not None
    assert len(patch.add_entities) == 1
    entity = patch.add_entities[0]
    assert entity.id == "ent_abc"
    assert entity.entity_type == "candidate"
    assert entity.name == "Widget A"
    assert entity.attributes == {"item_name": "Widget A", "price_usd": "19.99"}
    assert len(patch.add_evidence) == 2
    assert all(ev.entity_id == "ent_abc" for ev in patch.add_evidence)
    assert all(ev.source_event_id == 42 for ev in patch.add_evidence)


def test_entity_patch_from_findings_falls_back_to_summary_or_subgoal_for_name():
    structured = {"summary": "a page about something", "findings": [{"field": "rating", "value": "4.5"}]}
    patch = workspace_ops.entity_patch_from_findings(
        structured, entity_id="ent_x", subgoal="the subgoal text", source_event_id=1, source_url=None,
    )
    assert patch.add_entities[0].name == "a page about something"

    structured_no_summary = {"findings": [{"field": "rating", "value": "4.5"}]}
    patch2 = workspace_ops.entity_patch_from_findings(
        structured_no_summary, entity_id="ent_y", subgoal="the subgoal text", source_event_id=1, source_url=None,
    )
    assert patch2.add_entities[0].name == "the subgoal text"


def test_parse_requested_top_k_matches_generic_phrasings():
    assert workspace_ops.parse_requested_top_k("Find the 3 best vacuum cleaners") == 3
    assert workspace_ops.parse_requested_top_k("Find the 3 cheapest laptops and report them") == 3
    assert workspace_ops.parse_requested_top_k("Give me the top 5 hotels near downtown") == 5
    assert workspace_ops.parse_requested_top_k("List the top-rated 2 internships") == 2
    assert workspace_ops.parse_requested_top_k("Find the 4 highest-rated papers") == 4


def test_parse_requested_top_k_none_when_no_pattern():
    assert workspace_ops.parse_requested_top_k("Tell me what this page is about") is None
    assert workspace_ops.parse_requested_top_k("Go to example.com and log in") is None


def test_parse_requested_top_k_rejects_out_of_range():
    assert workspace_ops.parse_requested_top_k("Find the top 0 widgets") is None
    assert workspace_ops.parse_requested_top_k("Find the top 99 widgets") is None


def test_parse_requested_top_k_matches_arbitrary_regular_superlatives():
    """Regression for a live gap (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3 continued-
    validation forensic trace): the original fixed word list didn't cover "highest-paying" at
    all, so a live planner subgoal using it never qualified for the top-k evidence bypass or the
    goal-level deterministic-selection path. Regular English superlatives share one
    morphological marker (the "-est" suffix) matched structurally, not per-word, so any
    attribute name composes correctly without being individually hardcoded."""
    assert workspace_ops.parse_requested_top_k(
        "Identify the 2 highest-paying internship listings and report them with evidence"
    ) == 2
    assert workspace_ops.parse_requested_top_k("Find the 3 largest venues") == 3
    assert workspace_ops.parse_requested_top_k("List the 2 newest releases") == 2


def test_parse_requested_top_k_matches_relative_clause_shape():
    """A live planner subgoal separated the count from the superlative across a noun phrase
    ("the two readings WITH the smallest due_in_days") rather than placing them adjacent —
    still purely structural (a bounded "count ... with/having SUPERLATIVE" shape), never a
    domain/field keyword."""
    assert workspace_ops.parse_requested_top_k(
        "Identify the two readings with the smallest due_in_days values from the recorded readings"
    ) == 2
    assert workspace_ops.parse_requested_top_k(
        "Report the 3 candidates having the highest overall score"
    ) == 3


def test_coerce_structured_result_prefers_existing_typed_findings():
    structured = {"findings": [{"field": "price", "value": "10"}]}
    assert workspace_ops.coerce_structured_result(structured, "irrelevant free text") is structured


def test_coerce_structured_result_parses_json_embedded_in_result_text():
    text = '{"findings":[{"field":"name","value":"Widget A"},{"field":"price_usd","value":"19.99"}]}'
    coerced = workspace_ops.coerce_structured_result(None, text)
    assert coerced is not None
    assert [f["field"] for f in coerced["findings"]] == ["name", "price_usd"]


def test_coerce_structured_result_falls_back_to_key_value_prose():
    text = 'AeroClean 200: price_usd="$89.99", rating="3.9/5"'
    coerced = workspace_ops.coerce_structured_result(None, text)
    assert coerced is not None
    by_field = {f["field"]: f["value"] for f in coerced["findings"]}
    assert by_field == {"name": "AeroClean 200", "price_usd": "$89.99", "rating": "3.9/5"}
    assert all(f["evidence"] for f in coerced["findings"])


def test_coerce_structured_result_none_for_plain_prose_with_no_kv_shape():
    assert workspace_ops.coerce_structured_result(None, "The requested result is visible.") is None
    assert workspace_ops.coerce_structured_result(None, "Done.") is None
    assert workspace_ops.coerce_structured_result(None, "") is None


def test_coerce_structured_result_does_not_treat_a_generic_leading_word_as_a_name():
    """Regression for a live poisoning bug (docs/BROWSERAGENT_MASTER_STATUS.md's Phase 3
    continued-validation forensic trace): Qwen3-8B's own prose commonly opens with an ordinary
    verb ("Recorded price_per_night_usd: $219.00, rating: 4.6/5 for Cedar Plaza Hotel.") that is
    not the candidate's name — accepting it verbatim as a `name` finding fed a bogus single-word
    identity into agent/controller.py's infer_entity_name (which prefers ANY identity-field
    value over guessing from the subgoal text once one exists), which from there wrongly flagged
    a genuinely correct, on-the-right-page finish as stale evidence. Only text that itself looks
    like a real title (the same capitalized-multi-word-phrase shape used elsewhere in this
    module) is accepted as a name finding."""
    text = "Recorded price_per_night_usd: $219.00, rating: 4.6/5 for Cedar Plaza Hotel."
    coerced = workspace_ops.coerce_structured_result(None, text)
    assert coerced is not None
    fields = {f["field"] for f in coerced["findings"]}
    assert "name" not in fields
    by_field = {f["field"]: f["value"] for f in coerced["findings"]}
    assert by_field["price_per_night_usd"] == "$219.00"

    # A genuine leading title-shaped phrase is still accepted (unchanged behavior).
    still_works = workspace_ops.coerce_structured_result(None, 'AeroClean 200: price_usd="$89.99", rating="3.9/5"')
    assert still_works["findings"][0] == {
        "field": "name", "value": "AeroClean 200", "evidence": "AeroClean 200",
    }


def test_render_entities_report_lists_selected_entities_only_with_rationale():
    entities = [
        _entity("a", name="Widget A", price=10),
        _entity("b", name="Widget B", price=15),
    ]
    report = workspace_ops.render_entities_report(entities, {"a": "cheapest"})
    assert "1. Widget A (price=10) — cheapest" in report
    assert "2. Widget B (price=15)" in report
    assert "Widget C" not in report
