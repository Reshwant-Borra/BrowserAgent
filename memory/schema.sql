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

CREATE TABLE IF NOT EXISTS task_summaries (
    task_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,  -- JSON array
    covered_event_id INTEGER NOT NULL,
    compaction_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source_event_id INTEGER NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_memories_unique
ON task_memories(task_id, kind, content, source_event_id);

CREATE INDEX IF NOT EXISTS idx_task_memories_task
ON task_memories(task_id, source_event_id);

CREATE VIRTUAL TABLE IF NOT EXISTS task_memories_fts
USING fts5(content, kind, content='task_memories', content_rowid='id');

CREATE TABLE IF NOT EXISTS task_memory_ingest_state (
    task_id TEXT PRIMARY KEY,
    last_ingested_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS active_task_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_event_id INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_task_facts_unique
ON active_task_facts(task_id, kind, key, value, source_event_id);

CREATE INDEX IF NOT EXISTS idx_active_task_facts_task
ON active_task_facts(task_id, status, source_event_id);

-- Phase 1 (general-controller migration): TaskWorkspace projection tables. Like task_state,
-- these are derived/rebuildable views over the `events` table (source event: WORKSPACE_MUTATED
-- payload = a validated WorkspacePatch). Never written to directly by the model — only by
-- deterministic code in memory/workspace_store.py after validating a WorkspacePatch and
-- appending the corresponding event. See BrowserAgent_General_Autonomous_Agent_Architecture
-- REVISED.pdf, section 6.

CREATE TABLE IF NOT EXISTS workspace_state (
    task_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    data TEXT NOT NULL,              -- JSON object: open_questions, completion_requirements, etc.
    last_event_id INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS workspace_entities (
    id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    name TEXT,
    attributes TEXT NOT NULL,        -- JSON object
    status TEXT NOT NULL DEFAULT 'active',
    source_event_id INTEGER NOT NULL,
    updated_event_id INTEGER NOT NULL,
    PRIMARY KEY (task_id, id),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_entities_task
ON workspace_entities(task_id, entity_type, status);

CREATE TABLE IF NOT EXISTS workspace_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    entity_id TEXT,
    fact_key TEXT,
    field_key TEXT,
    source_event_id INTEGER NOT NULL,
    source_url TEXT,
    excerpt TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    verification_status TEXT NOT NULL DEFAULT 'observed',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_evidence_task
ON workspace_evidence(task_id, source_event_id);
