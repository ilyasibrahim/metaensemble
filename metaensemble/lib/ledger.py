"""Ledger access for MetaEnsemble.

All Ledger reads go through named functions in this module. Ad-hoc SQL
elsewhere is forbidden by PERFORMANCE.md §3 R1. Each query function declares
its complexity bound and the index it depends on.

State files:
- SQLite live cache:    .metaensemble/state/department.db
- Append-only mirror:   .metaensemble/state/runs.jsonl

The mirror is the source of truth for replay if the SQLite is lost.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# --- Stable Ledger literals ----------------------------------------------


OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_PARTIAL = "partial"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_BUDGET_EXCEEDED = "budget_exceeded"
OUTCOME_RECORDING_FAILED = "recording_failed"

ALLOWED_RUN_OUTCOMES = frozenset({
    OUTCOME_OK,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_INTERRUPTED,
    OUTCOME_BUDGET_EXCEEDED,
    OUTCOME_RECORDING_FAILED,
})


# --- Data classes --------------------------------------------------------


@dataclass(frozen=True)
class PostTaskFailedLogEntry:
    """One hook-log entry proving PostToolUse reached MetaEnsemble and failed."""

    ts: str | None
    run_id: str | None
    message: str


@dataclass(frozen=True)
class Run:
    """One execution attempt by one Executor for one Task.

    Fields beyond the 001-schema set are backfilled additively (TEXT
    columns + INTEGER counters with defaults) so existing Ledgers
    migrate without table rebuilds. The provenance fields are populated
    when the runtime exposes the underlying information; the dataclass
    keeps them optional so older Run rows continue to decode cleanly.
    """

    run_id: str
    executor_id: str
    task_id: str
    model: str
    tokens_in: int
    tokens_out: int
    window_id: str
    started_ts: str
    ended_ts: str
    outcome: str
    brief_in_path: str | None = None
    brief_out_path: str | None = None
    deliverable_path: str | None = None
    failure_reason: str | None = None
    quality_state: str | None = None
    quality_findings_json: str | None = None
    # 003_run_provenance additions:
    role_version: str | None = None
    requested_model_tier: str | None = None
    model_source: str | None = None
    deliverable_ref_json: str | None = None
    files_touched_json: str | None = None
    tool_use_json: str | None = None
    review_findings_json: str | None = None
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    orchestration_tokens: int = 0


def _parse_iso_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_post_task_failed_log_entries(
    log_path: Path,
    *,
    since: datetime | None = None,
) -> list[PostTaskFailedLogEntry]:
    """Read `post-task-failed` hook-log entries.

    Malformed log lines are ignored. Entries with an unparseable timestamp are
    kept when `since` is supplied because they are still actionable evidence.
    """
    if not log_path.exists():
        return []
    out: list[PostTaskFailedLogEntry] = []
    try:
        lines = log_path.read_text().splitlines()
    except OSError:
        return []
    cutoff = since.astimezone(timezone.utc) if since else None
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("kind") != "post-task-failed":
            continue
        ts_raw = record.get("ts")
        ts_dt = _parse_iso_datetime(ts_raw)
        if cutoff is not None and ts_dt is not None and ts_dt < cutoff:
            continue
        context = record.get("context")
        run_id = context.get("run_id") if isinstance(context, dict) else None
        message = record.get("message")
        out.append(PostTaskFailedLogEntry(
            ts=ts_raw if isinstance(ts_raw, str) else None,
            run_id=run_id if isinstance(run_id, str) and run_id else None,
            message=message if isinstance(message, str) else str(message or ""),
        ))
    return out


def recording_failure_reason(entry: PostTaskFailedLogEntry) -> str:
    """Stable failure_reason for rows whose recording pipeline failed."""
    message = entry.message.strip()
    if len(message) > 500:
        message = message[:497] + "..."
    if entry.ts and message:
        return f"post-task recording failed at {entry.ts}: {message}"
    if message:
        return f"post-task recording failed: {message}"
    return "post-task recording failed"


@dataclass(frozen=True)
class Executor:
    """A live, addressable instance of a Role."""

    executor_id: str
    alias: str
    role_id: str
    parent_executor_id: str | None
    created_ts: str
    last_seen_ts: str
    status: str


@dataclass(frozen=True)
class Role:
    """A registered Role specification."""

    role_id: str
    version: str
    spec_path: str
    model_tier: str
    created_ts: str


@dataclass(frozen=True)
class WindowSummary:
    """Aggregate token burn for a 5-hour window."""

    window_id: str
    total_runs: int
    total_tokens_in: int
    total_tokens_out: int


_REQUIRED_TEXT_RUN_FIELDS = (
    "run_id", "executor_id", "task_id", "model", "window_id",
    "started_ts", "ended_ts", "outcome",
)
_OPTIONAL_TEXT_RUN_FIELDS = (
    "brief_in_path", "brief_out_path", "deliverable_path", "failure_reason",
    "quality_state", "role_version", "requested_model_tier", "model_source",
)
_JSON_TEXT_RUN_FIELDS = (
    "quality_findings_json", "deliverable_ref_json", "files_touched_json",
    "tool_use_json", "review_findings_json",
)
_INTEGER_RUN_FIELDS = (
    "tokens_in", "tokens_out", "cache_read_tokens", "cache_create_tokens",
    "orchestration_tokens",
)


def _validate_run_for_persistence(run: Run) -> None:
    """Fail before SQLite binding if a Run violates the Ledger scalar contract."""
    errors: list[str] = []

    for field in _REQUIRED_TEXT_RUN_FIELDS:
        value = getattr(run, field)
        if not isinstance(value, str) or not value:
            errors.append(f"{field} must be a non-empty str, got {type(value).__name__}")

    for field in _OPTIONAL_TEXT_RUN_FIELDS:
        value = getattr(run, field)
        if value is not None and not isinstance(value, str):
            errors.append(f"{field} must be str|None, got {type(value).__name__}")

    for field in _JSON_TEXT_RUN_FIELDS:
        value = getattr(run, field)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{field} must be JSON text or None, got {type(value).__name__}")
            continue
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            errors.append(f"{field} must parse as JSON text: {exc.msg}")

    for field in _INTEGER_RUN_FIELDS:
        value = getattr(run, field)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{field} must be int, got {type(value).__name__}")

    if isinstance(run.outcome, str) and run.outcome not in ALLOWED_RUN_OUTCOMES:
        errors.append(
            "outcome must be one of "
            f"{sorted(ALLOWED_RUN_OUTCOMES)}, got {run.outcome!r}"
        )

    if errors:
        raise ValueError("invalid Run for Ledger persistence: " + "; ".join(errors))


# --- Ledger -------------------------------------------------------------


class Ledger:
    """SQLite + JSONL ledger. One instance per process; reuse across queries.

    PERFORMANCE.md R4 (connection pooling): one connection per process,
    opened once. Hooks that fork into subprocesses each open their own.
    """

    def __init__(self, db_path: Path | str, jsonl_path: Path | str):
        self.db_path = Path(db_path)
        self.jsonl_path = Path(jsonl_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self._conn.close()

    def initialize(self, migration_sql: str) -> None:
        """Apply a migration script. Idempotent (CREATE TABLE IF NOT EXISTS).

        Also applies backward-compatible column additions and the
        CHECK-constraint rebuild for Ledgers created before those
        constraints were introduced. SQLite has no `ALTER TABLE ADD COLUMN
        IF NOT EXISTS`, so we read the live schema via `PRAGMA table_info`
        and only run the ALTER when the column is actually missing. The
        CHECK-constraint rebuild uses the standard create-copy-drop-rename
        pattern, gated on a substring check against `sqlite_master` so a
        second initialize() call is a no-op.
        """
        self._conn.executescript(migration_sql)
        # First pass adds the columns the 002 rebuild needs to copy
        # (failure_reason, quality_state, quality_findings_json on
        # pre-002 databases).
        self._add_missing_columns()
        # Now the rebuild can safely copy those columns into the new
        # runs table with the extended CHECK constraint.
        self._apply_outcome_extended_migration()
        # Re-run the backfill so the post-002 schema picks up the 003
        # columns the rebuild dropped on its way through. Idempotent —
        # `PRAGMA table_info` skips any column already present.
        self._add_missing_columns()
        self._reclassify_recording_failed_salvage_rows()

    # Columns introduced after 001_init.sql. Each entry is
    # (table, column_name, column_type). New entries appended over time;
    # the loop handles each idempotently against PRAGMA table_info.
    _BACKFILL_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("runs", "failure_reason", "TEXT"),
        ("runs", "quality_state", "TEXT"),
        ("runs", "quality_findings_json", "TEXT"),
        # 003_run_provenance: additive provenance + token-economics columns.
        ("runs", "role_version", "TEXT"),
        ("runs", "requested_model_tier", "TEXT"),
        ("runs", "model_source", "TEXT"),
        ("runs", "deliverable_ref_json", "TEXT"),
        ("runs", "files_touched_json", "TEXT"),
        ("runs", "tool_use_json", "TEXT"),
        ("runs", "review_findings_json", "TEXT"),
        ("runs", "cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("runs", "cache_create_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("runs", "orchestration_tokens", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _add_missing_columns(self) -> None:
        for table, column, column_type in self._BACKFILL_COLUMNS:
            existing = {
                row[1]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )

    def _apply_outcome_extended_migration(self) -> None:
        """Apply `002_outcome_extended.sql` if the runs table predates it.

        The 002 migration cannot live inside the canonical migration_sql
        because it issues `PRAGMA foreign_keys = OFF` outside any
        transaction, which SQLite refuses inside `executescript`. We
        guard on the table DDL stored in `sqlite_master.sql`: if the
        extended outcome literals are already in the runs table's CREATE
        statement, the migration has already been applied and we skip.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if row is None:
            return
        existing_ddl = row[0] or ""
        if (
            "'interrupted'" in existing_ddl
            and "'budget_exceeded'" in existing_ddl
            and "'recording_failed'" in existing_ddl
        ):
            return
        migrations_dir = (
            Path(__file__).resolve().parent.parent / "state" / "migrations"
        )
        script = (migrations_dir / "002_outcome_extended.sql").read_text()
        # `executescript` cannot wrap a PRAGMA-foreign-keys-OFF / BEGIN /
        # COMMIT sequence cleanly, so run statements separately.
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.executescript(script)
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    def _hook_log_path_for_db(self) -> Path:
        """Return the hook log path for the project that owns this DB."""
        return self.db_path.parent.parent / "hooks" / "log.jsonl"

    def _reclassify_recording_failed_salvage_rows(self) -> None:
        """Promote mislabeled interrupted salvage rows to recording_failed."""
        entries = {
            entry.run_id: entry
            for entry in read_post_task_failed_log_entries(self._hook_log_path_for_db())
            if entry.run_id
        }
        if not entries:
            return
        with self.transaction() as conn:
            for run_id, entry in entries.items():
                conn.execute(
                    """
                    UPDATE runs
                    SET outcome = ?, failure_reason = ?
                    WHERE run_id = ? AND outcome = ?
                    """,
                    (
                        OUTCOME_RECORDING_FAILED,
                        recording_failure_reason(entry),
                        run_id,
                        OUTCOME_INTERRUPTED,
                    ),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection. Auto-commit on success, rollback on error."""
        try:
            self._conn.execute("BEGIN")
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # --- Writes ---------------------------------------------------------

    def run_exists(self, run_id: str) -> bool:
        """True when a Run with this run_id is already recorded in SQLite."""
        cur = self._conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ? LIMIT 1", (run_id,)
        )
        return cur.fetchone() is not None

    def append_run(self, run: Run) -> bool:
        """Append a Run to SQLite and JSONL, idempotently by run_id.

        Returns True when a new row was inserted, False when a row with this
        run_id already existed (insert skipped via ON CONFLICT). The JSONL
        mirror is appended *only on a real insert*, so re-finalizing or
        re-reconciling the same run_id can never raise on the PRIMARY KEY and
        never duplicates the JSONL mirror line.

        SQLite write is transactional. JSONL append happens after SQLite
        commits, so an orphaned JSONL line (without a SQLite row) is the
        only failure mode, which `replay_from_jsonl` cleans up safely.
        """
        _validate_run_for_persistence(run)
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs (
                    run_id, executor_id, task_id, model,
                    tokens_in, tokens_out, window_id,
                    started_ts, ended_ts, outcome,
                    brief_in_path, brief_out_path, deliverable_path,
                    failure_reason,
                    quality_state, quality_findings_json,
                    role_version, requested_model_tier, model_source,
                    deliverable_ref_json,
                    files_touched_json, tool_use_json, review_findings_json,
                    cache_read_tokens, cache_create_tokens, orchestration_tokens
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    run.run_id, run.executor_id, run.task_id, run.model,
                    run.tokens_in, run.tokens_out, run.window_id,
                    run.started_ts, run.ended_ts, run.outcome,
                    run.brief_in_path, run.brief_out_path, run.deliverable_path,
                    run.failure_reason,
                    run.quality_state, run.quality_findings_json,
                    run.role_version,
                    run.requested_model_tier, run.model_source,
                    run.deliverable_ref_json,
                    run.files_touched_json, run.tool_use_json, run.review_findings_json,
                    run.cache_read_tokens, run.cache_create_tokens,
                    run.orchestration_tokens,
                ),
            )
            inserted = cur.rowcount == 1
        if inserted:
            with self.jsonl_path.open("a") as f:
                f.write(json.dumps(asdict(run)) + "\n")
        return inserted

    def upsert_executor(self, executor: Executor) -> None:
        """Insert or update an Executor by executor_id."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO executors (
                    executor_id, alias, role_id, parent_executor_id,
                    created_ts, last_seen_ts, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(executor_id) DO UPDATE SET
                    last_seen_ts = excluded.last_seen_ts,
                    status = excluded.status
                """,
                (
                    executor.executor_id, executor.alias, executor.role_id,
                    executor.parent_executor_id, executor.created_ts,
                    executor.last_seen_ts, executor.status,
                ),
            )

    def ensure_role(
        self,
        *,
        role_id: str,
        version: str,
        spec_path: str,
        model_tier: str,
        created_ts: str | None = None,
    ) -> None:
        """O(log N) insert-if-absent using roles primary key."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO roles "
                "(role_id, version, spec_path, model_tier, created_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    role_id,
                    version,
                    spec_path,
                    model_tier,
                    created_ts or datetime.now().isoformat(),
                ),
            )

    def ensure_task(
        self,
        *,
        task_id: str,
        task_type: str,
        status: str,
        manifest_path: str | None = None,
        parent_task_id: str | None = None,
        created_ts: str | None = None,
    ) -> None:
        """O(log N) insert-if-absent using tasks primary key."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(task_id, task_type, status, manifest_path, parent_task_id, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    task_type,
                    status,
                    manifest_path,
                    parent_task_id,
                    created_ts or datetime.now().isoformat(),
                ),
            )

    # --- Named queries (PERFORMANCE.md R1 + R5) -------------------------

    def get_recent_runs(
        self, limit: int = 50, since: datetime | None = None
    ) -> list[Run]:
        """O(log N + K) using idx_runs_ended_ts. K = limit (default 50)."""
        if since is not None:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE ended_ts >= ? "
                "ORDER BY ended_ts DESC LIMIT ?",
                (since.isoformat(), limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY ended_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Run(**dict(row)) for row in rows]

    def get_runs_by_executor(self, executor_id: str, limit: int = 50) -> list[Run]:
        """O(log N + K) using idx_runs_executor."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE executor_id = ? "
            "ORDER BY ended_ts DESC LIMIT ?",
            (executor_id, limit),
        ).fetchall()
        return [Run(**dict(row)) for row in rows]

    def count_runs_by_executor(self, executor_id: str) -> int:
        """O(log N) using idx_runs_executor."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM runs WHERE executor_id = ?",
            (executor_id,),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def get_runs_by_task(self, task_id: str, limit: int = 50) -> list[Run]:
        """O(log N + K) using idx_runs_task."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE task_id = ? "
            "ORDER BY ended_ts DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [Run(**dict(row)) for row in rows]

    def get_window_burn(self, window_id: str) -> WindowSummary:
        """O(K) using idx_runs_window. K = runs in window."""
        row = self._conn.execute(
            """
            SELECT COUNT(*)               AS cnt,
                   COALESCE(SUM(tokens_in),  0) AS ti,
                   COALESCE(SUM(tokens_out), 0) AS tos
            FROM runs WHERE window_id = ?
            """,
            (window_id,),
        ).fetchone()
        return WindowSummary(
            window_id=window_id,
            total_runs=row["cnt"],
            total_tokens_in=row["ti"],
            total_tokens_out=row["tos"],
        )

    def get_executor_by_alias(self, alias: str) -> Executor | None:
        """O(log N) using idx_executors_alias."""
        row = self._conn.execute(
            "SELECT * FROM executors WHERE alias = ?",
            (alias,),
        ).fetchone()
        return Executor(**dict(row)) if row else None

    def get_executor(self, executor_id: str) -> Executor | None:
        """O(log N) using executors primary key."""
        row = self._conn.execute(
            "SELECT * FROM executors WHERE executor_id = ?",
            (executor_id,),
        ).fetchone()
        return Executor(**dict(row)) if row else None

    def get_active_executors(
        self, since: datetime, limit: int = 50
    ) -> list[Executor]:
        """O(log N + K) using idx_executors_last_seen."""
        rows = self._conn.execute(
            "SELECT * FROM executors WHERE last_seen_ts >= ? "
            "ORDER BY last_seen_ts DESC LIMIT ?",
            (since.isoformat(), limit),
        ).fetchall()
        return [Executor(**dict(row)) for row in rows]

    def get_role(self, role_id: str) -> Role | None:
        """O(log N) using roles primary key."""
        row = self._conn.execute(
            "SELECT * FROM roles WHERE role_id = ?",
            (role_id,),
        ).fetchone()
        return Role(**dict(row)) if row else None

    def get_active_executor_for_role(self, role_id: str) -> Executor | None:
        """O(log N + K) using idx_executors_role; K = active executors for role."""
        row = self._conn.execute(
            "SELECT * FROM executors WHERE role_id = ? AND status = 'active' "
            "ORDER BY last_seen_ts DESC LIMIT 1",
            (role_id,),
        ).fetchone()
        return Executor(**dict(row)) if row else None

    def get_related_executors(
        self, executor: Executor, limit: int = 50
    ) -> list[Executor]:
        """O(log N + K) using executor primary key and parent_executor_id scan.

        v0.1.0 does not index `parent_executor_id` because fan-out groups are
        small. The limit bounds the scan result used by relaunch context.
        """
        if not executor.parent_executor_id:
            return []
        related: list[Executor] = []
        seen: set[str] = {executor.executor_id}

        rows = self._conn.execute(
            "SELECT * FROM executors WHERE parent_executor_id = ? "
            "AND executor_id != ? "
            "ORDER BY created_ts DESC LIMIT ?",
            (executor.parent_executor_id, executor.executor_id, limit),
        ).fetchall()
        for row in rows:
            if row["executor_id"] in seen:
                continue
            related.append(Executor(**dict(row)))
            seen.add(row["executor_id"])

        parent = self.get_executor(executor.parent_executor_id)
        if parent is not None and parent.executor_id not in seen:
            related.append(parent)
        return related

    def count_pattern_runs(self, role_id: str, task_type: str) -> int:
        """O(K) bounded by runs for role/task pattern via indexed joins."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM runs r
            INNER JOIN tasks t ON t.task_id = r.task_id
            INNER JOIN executors e ON e.executor_id = r.executor_id
            WHERE e.role_id = ? AND t.task_type = ?
            """,
            (role_id, task_type),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def update_task_status(self, task_id: str, status: str) -> None:
        """O(log N) using tasks primary key."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (status, task_id),
            )

    # --- Replay --------------------------------------------------------

    def replay_from_jsonl(self) -> int:
        """Reconstruct SQLite Runs from runs.jsonl. Returns lines processed.

        Idempotent via INSERT OR IGNORE: running twice produces the same
        SQLite state. Use after a database loss or migration. The caller
        is responsible for restoring `roles`, `executors`, and `tasks`
        from their respective sources before invoking this.
        """
        if not self.jsonl_path.exists():
            return 0
        count = 0
        with self.jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                with self.transaction() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO runs (
                            run_id, executor_id, task_id, model,
                            tokens_in, tokens_out, window_id,
                            started_ts, ended_ts, outcome,
                            brief_in_path, brief_out_path, deliverable_path,
                            failure_reason,
                            quality_state, quality_findings_json,
                            role_version, requested_model_tier, model_source,
                            deliverable_ref_json,
                            files_touched_json, tool_use_json, review_findings_json,
                            cache_read_tokens, cache_create_tokens, orchestration_tokens
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            d["run_id"], d["executor_id"], d["task_id"], d["model"],
                            d["tokens_in"], d["tokens_out"], d["window_id"],
                            d["started_ts"], d["ended_ts"], d["outcome"],
                            d.get("brief_in_path"),
                            d.get("brief_out_path"),
                            d.get("deliverable_path"),
                            d.get("failure_reason"),
                            d.get("quality_state"),
                            d.get("quality_findings_json"),
                            d.get("role_version"),
                            d.get("requested_model_tier"),
                            d.get("model_source"),
                            d.get("deliverable_ref_json"),
                            d.get("files_touched_json"),
                            d.get("tool_use_json"),
                            d.get("review_findings_json"),
                            int(d.get("cache_read_tokens") or 0),
                            int(d.get("cache_create_tokens") or 0),
                            int(d.get("orchestration_tokens") or 0),
                        ),
                    )
                count += 1
        return count
