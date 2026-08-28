"""Pydantic contracts for the general-controller TaskWorkspace (Phase 1). These are the only
shapes the model is ever allowed to produce when it wants to mutate task-local workspace
state — it emits a WorkspacePatch; memory/workspace_store.py validates it, appends one
WORKSPACE_MUTATED event, and updates the projection tables. The model never writes SQL.
See BrowserAgent_General_Autonomous_Agent_Architecture_REVISED.pdf, section 6.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

EntityStatus = Literal["active", "rejected", "selected", "resolved"]

JsonValue = Any  # pydantic has no built-in recursive JSON type alias pre-2.x codegen; kept loose intentionally


class WorkspaceEntity(BaseModel):
    id: str
    entity_type: str
    name: Optional[str] = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    status: EntityStatus = "active"


class WorkspaceEntityPatch(BaseModel):
    """Partial update to an existing entity, addressed by id. Only provided fields change."""
    id: str
    name: Optional[str] = None
    attributes: Optional[dict[str, JsonValue]] = None
    status: Optional[EntityStatus] = None


class WorkspaceFact(BaseModel):
    key: str
    value: JsonValue


class EvidenceRef(BaseModel):
    entity_id: Optional[str] = None
    fact_key: Optional[str] = None
    field_key: Optional[str] = None
    source_event_id: int
    source_url: Optional[str] = None
    excerpt: str
    confidence: float = Field(ge=0, le=1, default=1.0)


class WorkspacePatch(BaseModel):
    """The sole mutation surface for the TaskWorkspace. Deterministic code (workspace_store.py)
    validates every field (referenced entity ids exist, source_event_id exists for this task,
    confidence in range) before it is ever persisted."""
    add_entities: list[WorkspaceEntity] = Field(default_factory=list)
    update_entities: list[WorkspaceEntityPatch] = Field(default_factory=list)
    add_facts: list[WorkspaceFact] = Field(default_factory=list)
    add_evidence: list[EvidenceRef] = Field(default_factory=list)
    open_questions_add: list[str] = Field(default_factory=list)
    open_questions_resolve: list[str] = Field(default_factory=list)
    completion_requirements: Optional[list[str]] = None


class WorkspaceView(BaseModel):
    """Read-side projection returned by WorkspaceStore.load() — the rebuildable, in-memory
    view assembled from workspace_state + workspace_entities + workspace_evidence."""
    task_id: str
    version: int = 1
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    completion_requirements: list[str] = Field(default_factory=list)
    entities: list[WorkspaceEntity] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    last_event_id: Optional[int] = None
