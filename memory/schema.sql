-- events is the sole source of truth (append-only, never UPDATEd/DELETEd in normal operation).
-- task_state is a derived/materialized view: rebuildable at any time by replaying events
-- (see memory/replay.py). If task_state is missing or its last_event_id is behind the max
-- event id for a task, it is stale and must be rebuilt before use — never trusted blindly.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    goal TEXT NOT NULL,
    success_criteria TEXT NOT NULL  -- JSON array of strings
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,           -- JSON
    verification_result TEXT,        -- JSON or NULL
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    current_step INTEGER NOT NULL,
    current_subgoal TEXT,
    plan TEXT,                       -- JSON array of strings
    completed_subgoals TEXT,         -- JSON array of strings
    current_url TEXT,
    current_page_hash TEXT,
    recent_actions TEXT,             -- JSON array of small dicts (sliding window)
    recovery_level TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    status TEXT NOT NULL,
    last_event_id INTEGER,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
