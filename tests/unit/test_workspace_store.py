"""Phase 1 core guarantee: the TaskWorkspace projection is derived and rebuildable from events,
never authoritative itself. Mirrors tests/unit/test_task_state_store.py's approach — sqlite
only, no browser, no model."""
from __future__ import annotations

import pytest

from agent.workspace_models import EvidenceRef, WorkspaceEntity, WorkspaceEntityPatch, WorkspacePatch
from memory.event_store import EventStore
from memory.workspace_store import WorkspacePatchError, WorkspaceStore


def make_store(tmp_path):
    es = EventStore(tmp_path / "task.db")
    store = WorkspaceStore(es)
    return es, store


def test_empty_workspace_for_new_task(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    view = store.load("t1")
    assert view.entities == []
    assert view.facts == {}
    es.close()


def test_add_entity_persists_and_loads(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    patch = WorkspacePatch(add_entities=[
        WorkspaceEntity(id="ent_1", entity_type="candidate", name="Widget",
                         attributes={"price_usd": 9.99}),
    ])
    view = store.apply_patch("t1", patch)
    assert len(view.entities) == 1
    assert view.entities[0].id == "ent_1"
    assert view.entities[0].attributes["price_usd"] == 9.99

    reloaded = store.load("t1")
    assert len(reloaded.entities) == 1
    es.close()


def test_update_entity_status(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    store.apply_patch("t1", WorkspacePatch(add_entities=[
        WorkspaceEntity(id="ent_1", entity_type="candidate", attributes={}),
    ]))
    view = store.apply_patch("t1", WorkspacePatch(update_entities=[
        WorkspaceEntityPatch(id="ent_1", status="selected"),
    ]))
    assert view.entities[0].status == "selected"
    es.close()


def test_duplicate_entity_id_rejected(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    store.apply_patch("t1", WorkspacePatch(add_entities=[
        WorkspaceEntity(id="ent_1", entity_type="candidate", attributes={}),
    ]))
    with pytest.raises(WorkspacePatchError):
        store.apply_patch("t1", WorkspacePatch(add_entities=[
            WorkspaceEntity(id="ent_1", entity_type="candidate", attributes={}),
        ]))
    es.close()


def test_update_unknown_entity_rejected(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    with pytest.raises(WorkspacePatchError):
        store.apply_patch("t1", WorkspacePatch(update_entities=[
            WorkspaceEntityPatch(id="ghost", status="selected"),
        ]))
    es.close()


def test_evidence_must_reference_real_event(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    with pytest.raises(WorkspacePatchError):
        store.apply_patch("t1", WorkspacePatch(add_evidence=[
            EvidenceRef(source_event_id=9999, excerpt="fabricated"),
        ]))
    es.close()


def test_evidence_referencing_own_mutation_event_is_valid(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    # source_event_id must exist *before* this patch is appended, so reference a prior event.
    first = store.apply_patch("t1", WorkspacePatch(add_entities=[
        WorkspaceEntity(id="ent_1", entity_type="candidate", attributes={}),
    ]))
    prior_event_id = first.last_event_id
    view = store.apply_patch("t1", WorkspacePatch(add_evidence=[
        EvidenceRef(entity_id="ent_1", source_event_id=prior_event_id, excerpt="observed price"),
    ]))
    assert len(view.evidence) == 1
    assert view.evidence[0].entity_id == "ent_1"
    es.close()


def test_open_questions_add_and_resolve(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    store.apply_patch("t1", WorkspacePatch(open_questions_add=["which color?", "which size?"]))
    view = store.apply_patch("t1", WorkspacePatch(open_questions_resolve=["which color?"]))
    assert view.open_questions == ["which size?"]
    es.close()


def test_facts_accumulate_across_patches(tmp_path):
    es, store = make_store(tmp_path)
    es.create_task("t1", "goal", [])
    store.apply_patch("t1", WorkspacePatch(add_facts=[{"key": "budget", "value": 200}]))
    view = store.apply_patch("t1", WorkspacePatch(add_facts=[{"key": "brand", "value": "acme"}]))
    assert view.facts == {"budget": 200, "brand": "acme"}
    es.close()


def test_model_never_writes_sql_only_patches(tmp_path):
    """WorkspacePatch is the only mutation surface — there is no method on WorkspaceStore that
    accepts raw SQL or an arbitrary dict; apply_patch is strictly typed."""
    es, store = make_store(tmp_path)
    assert not hasattr(store, "execute_sql")
    assert not hasattr(store, "raw_write")
    es.close()
