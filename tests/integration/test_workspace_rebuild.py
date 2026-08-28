"""Phase 1 PASS gate: 100% rebuild equivalence after deleting projection rows, and five
heterogeneous entity types require zero schema migrations (workspace_entities.attributes is
opaque JSON, so any entity_type/shape fits without a new column or table)."""
from __future__ import annotations

from agent.workspace_models import EvidenceRef, WorkspaceEntity, WorkspacePatch
from memory.event_store import EventStore
from memory.workspace_store import WorkspaceStore


def make_store(tmp_path):
    es = EventStore(tmp_path / "task.db")
    store = WorkspaceStore(es)
    return es, store


def test_rebuild_equivalence_after_deleting_projection_rows(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])

    store.apply_patch("t1", WorkspacePatch(add_entities=[
        WorkspaceEntity(id="ent_1", entity_type="candidate", name="A", attributes={"price": 10}),
        WorkspaceEntity(id="ent_2", entity_type="candidate", name="B", attributes={"price": 20}),
    ]))
    e1 = store.apply_patch("t1", WorkspacePatch(
        add_evidence=[EvidenceRef(entity_id="ent_1", source_event_id=es.max_event_id("t1"), excerpt="seen")],
        open_questions_add=["is B in stock?"],
        add_facts=[{"key": "budget", "value": 50}],
        completion_requirements=["pick cheapest"],
    ))
    original = store.load("t1")

    # Simulate projection loss (crash mid-write, disk corruption, manual DROP+CREATE, etc).
    es.conn.execute("DELETE FROM workspace_state WHERE task_id = ?", ("t1",))
    es.conn.execute("DELETE FROM workspace_entities WHERE task_id = ?", ("t1",))
    es.conn.execute("DELETE FROM workspace_evidence WHERE task_id = ?", ("t1",))
    es.conn.commit()

    rebuilt = store.load("t1")

    assert rebuilt.facts == original.facts
    assert rebuilt.open_questions == original.open_questions
    assert rebuilt.completion_requirements == original.completion_requirements
    assert {e.id for e in rebuilt.entities} == {e.id for e in original.entities}
    assert [e.attributes for e in rebuilt.entities] == [e.attributes for e in original.entities]
    assert len(rebuilt.evidence) == len(original.evidence)
    assert rebuilt.last_event_id == original.last_event_id
    es.close()


def test_five_heterogeneous_entity_types_no_schema_migration(tmp_path):
    """Product, college, hotel, internship, paper — all fit in the same generic
    workspace_entities table with no code change and no new table/column."""
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])

    heterogeneous = [
        WorkspaceEntity(id="p1", entity_type="product", name="Vacuum",
                         attributes={"price_usd": 159.99, "rating": 4.6, "features": ["cordless", "HEPA"]}),
        WorkspaceEntity(id="c1", entity_type="college", name="Example U",
                         attributes={"tuition_usd": 40000, "acceptance_rate": 0.12, "location": "CA"}),
        WorkspaceEntity(id="h1", entity_type="hotel", name="Example Inn",
                         attributes={"nightly_rate": 120, "stars": 4, "amenities": ["pool", "gym"]}),
        WorkspaceEntity(id="i1", entity_type="internship", name="Example Corp SWE Intern",
                         attributes={"paid": True, "duration_weeks": 12, "remote": False}),
        WorkspaceEntity(id="a1", entity_type="paper", name="Example Paper Title",
                         attributes={"authors": ["A. One", "B. Two"], "year": 2025, "citations": 42}),
    ]
    view = store.apply_patch("t1", WorkspacePatch(add_entities=heterogeneous))

    assert len(view.entities) == 5
    types_seen = {e.entity_type for e in view.entities}
    assert types_seen == {"product", "college", "hotel", "internship", "paper"}
    # Round-trip through rebuild too, to prove the projection tables (not just the in-memory
    # objects) tolerate arbitrary per-type attribute shapes.
    es.conn.execute("DELETE FROM workspace_state WHERE task_id = ?", ("t1",))
    es.conn.execute("DELETE FROM workspace_entities WHERE task_id = ?", ("t1",))
    es.conn.commit()
    rebuilt = store.load("t1")
    assert {e.id: e.attributes for e in rebuilt.entities} == {e.id: e.attributes for e in view.entities}
    es.close()


def test_projection_never_ahead_of_events_after_crash_before_commit(tmp_path):
    """apply_patch validates before writing anything; a rejected patch leaves projections
    exactly as they were (no partial application)."""
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    store.apply_patch("t1", WorkspacePatch(add_entities=[
        WorkspaceEntity(id="ent_1", entity_type="candidate", attributes={}),
    ]))
    before = store.load("t1")

    import pytest
    from memory.workspace_store import WorkspacePatchError
    with pytest.raises(WorkspacePatchError):
        store.apply_patch("t1", WorkspacePatch(add_entities=[
            WorkspaceEntity(id="ent_1", entity_type="candidate", attributes={}),  # duplicate -> rejected
            WorkspaceEntity(id="ent_2", entity_type="candidate", attributes={}),  # would-be valid, but atomic
        ]))

    after = store.load("t1")
    assert {e.id for e in after.entities} == {e.id for e in before.entities}
    es.close()
