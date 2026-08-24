"""AgentLoop.run() closes its own EventStore connection when the task run finishes (correct
production behavior — a one-shot CLI process should release its DB handle). Tests that want
to inspect events *after* run() completes must reopen the same on-disk database, exactly as
the CLI's `status` command and the Phase 3 resume path already do."""
from __future__ import annotations

from memory.event_store import Event, EventStore


def read_events(loop) -> list[Event]:
    es = EventStore(loop.db_path)
    try:
        return es.all_events(loop.task_id)
    finally:
        es.close()
