-- 002_outcome_extended.sql — extend the `runs.outcome` CHECK constraint to
-- recognize `interrupted`, `budget_exceeded`, and `recording_failed` as
-- first-class outcomes.
--
-- Background. The 001 schema's CHECK clause was `outcome IN ('ok','failed',
-- 'partial')`. The runtime records distinct ledger outcomes for
-- runs that ended without an Executor verdict — `interrupted` when the
-- session ended before PostToolUse fired, `budget_exceeded` when
-- `claude --max-budget-usd` killed the process before the run completed,
-- and `recording_failed` when PostToolUse reached MetaEnsemble but the
-- recording pipeline failed before the Run row was persisted.
-- SQLite cannot ALTER a CHECK constraint in place, so this migration uses
-- the standard rebuild pattern: copy into a new table, drop the old,
-- rename. Foreign keys are disabled for the duration so the cross-table
-- references on the existing `runs` rows do not block the rebuild.
--
-- Idempotent: the application-side guard in `Ledger.initialize` skips
-- this script when `sqlite_master.sql` for `runs` already mentions the
-- new outcome literals. The CREATE / INSERT / DROP / RENAME below is
-- safe to re-run on a fresh database only because the guard prevents
-- a no-op rebuild from racing against concurrent writes.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE runs_v2 (
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
  quality_state         TEXT,
  quality_findings_json TEXT,
  role_version          TEXT,
  requested_model_tier  TEXT,
  model_source          TEXT,
  deliverable_ref_json  TEXT,
  files_touched_json    TEXT,
  tool_use_json         TEXT,
  review_findings_json  TEXT,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_create_tokens   INTEGER NOT NULL DEFAULT 0,
  orchestration_tokens  INTEGER NOT NULL DEFAULT 0
);

INSERT INTO runs_v2 (
  run_id, executor_id, task_id, model, tokens_in, tokens_out, window_id,
  started_ts, ended_ts, outcome, brief_in_path, brief_out_path,
  deliverable_path, failure_reason, quality_state, quality_findings_json,
  role_version, requested_model_tier, model_source, deliverable_ref_json,
  files_touched_json, tool_use_json, review_findings_json,
  cache_read_tokens, cache_create_tokens, orchestration_tokens
)
SELECT
  run_id, executor_id, task_id, model, tokens_in, tokens_out, window_id,
  started_ts, ended_ts, outcome, brief_in_path, brief_out_path,
  deliverable_path, failure_reason, quality_state, quality_findings_json,
  role_version, requested_model_tier, model_source, deliverable_ref_json,
  files_touched_json, tool_use_json, review_findings_json,
  cache_read_tokens, cache_create_tokens, orchestration_tokens
FROM runs;

DROP TABLE runs;
ALTER TABLE runs_v2 RENAME TO runs;

CREATE INDEX IF NOT EXISTS idx_runs_window   ON runs(window_id);
CREATE INDEX IF NOT EXISTS idx_runs_executor ON runs(executor_id);
CREATE INDEX IF NOT EXISTS idx_runs_task     ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_ended_ts ON runs(ended_ts);

COMMIT;

PRAGMA foreign_keys = ON;
