-- 001_init.sql — Initial Ledger schema for MetaEnsemble.
-- Idempotent: safe to run on a fresh or already-initialized database.
-- See ARCHITECTURE.md §5 for the schema rationale and PERFORMANCE.md §3 R2
-- for the index-first design rule that this file enforces.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Roles: declarative specs registered in the system.
CREATE TABLE IF NOT EXISTS roles (
  role_id    TEXT PRIMARY KEY,
  version    TEXT NOT NULL,
  spec_path  TEXT NOT NULL,
  model_tier TEXT NOT NULL CHECK (model_tier IN ('opus', 'sonnet', 'haiku')),
  created_ts TEXT NOT NULL
);

-- Executors: live instances of Roles with stable identity across sessions.
CREATE TABLE IF NOT EXISTS executors (
  executor_id        TEXT PRIMARY KEY,
  alias              TEXT NOT NULL UNIQUE,
  role_id            TEXT NOT NULL REFERENCES roles(role_id),
  parent_executor_id TEXT REFERENCES executors(executor_id),
  created_ts         TEXT NOT NULL,
  last_seen_ts       TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('idle', 'active', 'retired'))
);

-- Tasks: units of work assigned to one or more Executors.
CREATE TABLE IF NOT EXISTS tasks (
  task_id        TEXT PRIMARY KEY,
  task_type      TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'done', 'failed')),
  manifest_path  TEXT,
  parent_task_id TEXT REFERENCES tasks(task_id),
  created_ts     TEXT NOT NULL
);

-- Runs: append-only execution log. The Ledger.
CREATE TABLE IF NOT EXISTS runs (
  run_id           TEXT PRIMARY KEY,
  executor_id      TEXT NOT NULL REFERENCES executors(executor_id),
  task_id          TEXT NOT NULL REFERENCES tasks(task_id),
  model            TEXT NOT NULL,
  tokens_in        INTEGER NOT NULL,
  tokens_out       INTEGER NOT NULL,
  window_id        TEXT NOT NULL,
  started_ts       TEXT NOT NULL,
  ended_ts         TEXT NOT NULL,
  outcome          TEXT NOT NULL CHECK (outcome IN (
                     'ok', 'failed', 'partial',
                     'interrupted', 'budget_exceeded', 'recording_failed'
                   )),
  brief_in_path         TEXT,
  brief_out_path        TEXT,
  deliverable_path      TEXT,
  failure_reason        TEXT,
  quality_state         TEXT,    -- auto | notify | block | (NULL when gate did not run)
  quality_findings_json TEXT     -- compact JSON: {axes: [{name, state, findings, raw}, ...]}
);

-- Indices required by PERFORMANCE.md R2. Every column used in WHERE,
-- ORDER BY, or JOIN gets an index.
CREATE INDEX IF NOT EXISTS idx_runs_window         ON runs(window_id);
CREATE INDEX IF NOT EXISTS idx_runs_executor       ON runs(executor_id);
CREATE INDEX IF NOT EXISTS idx_runs_task           ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_ended_ts       ON runs(ended_ts);
CREATE INDEX IF NOT EXISTS idx_executors_alias     ON executors(alias);
CREATE INDEX IF NOT EXISTS idx_executors_last_seen ON executors(last_seen_ts);
CREATE INDEX IF NOT EXISTS idx_executors_role      ON executors(role_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status        ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent        ON tasks(parent_task_id);
