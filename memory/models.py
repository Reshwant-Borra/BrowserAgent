"""TaskRecord (immutable `tasks` row) and TaskState (derived `task_state` row) — the shared
types between task_state.py (storage) and replay.py (reconstruction), split out to avoid a
storage<->replay import cycle."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskRecord(BaseModel):
    id: str
    created_at: str
    status: str
    goal: str
    success_criteria: list[str] = Field(default_factory=list)


class TaskState(BaseModel):
    task_id: str
    current_step: int = 0
    current_subgoal: Optional[str] = None
    plan: list[str] = Field(default_factory=list)
    completed_subgoals: list[str] = Field(default_factory=list)
    current_url: Optional[str] = None
    current_page_hash: Optional[str] = None
    recent_actions: list[dict[str, Any]] = Field(default_factory=list)  # bounded sliding window
    recovery_level: str = "normal"
    retry_count: int = 0
    blocked_reason: Optional[str] = None
    status: str = "running"
    last_event_id: Optional[int] = None
    pending_action_intent: Optional[dict[str, Any]] = None  # set between ACTION_INTENT and ACTION_RESULT
