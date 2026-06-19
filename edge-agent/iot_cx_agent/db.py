import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    status_code INTEGER,
    error TEXT,
    response_body TEXT
);

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_key TEXT NOT NULL UNIQUE,
    display_name TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL,
    sample_value TEXT,
    sample_time_utc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(point_id) REFERENCES edge_points(id)
);

CREATE TABLE IF NOT EXISTS trend_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_sec INTEGER NOT NULL DEFAULT 300,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(point_id) REFERENCES edge_points(id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT,
    payload_json TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
"""

JOB_COLUMNS = {
    "job_id": "TEXT",
    "request_json": "TEXT",
    "error_message": "TEXT",
    "claimed_at": "TEXT",
    "completed_at": "TEXT",
}


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        ensure_jobs_columns(conn)
        conn.commit()


def ensure_jobs_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for column_name, column_def in JOB_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_def}")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def queued_upload_count(path: Path) -> int:
    with connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM sync_queue WHERE status = 'pending'").fetchone()
        return int(row["count"])


def record_heartbeat_attempt(
    path: Path,
    attempted_at: str,
    success: bool,
    status_code: int | None = None,
    error: str | None = None,
    response_body: str | None = None,
) -> None:
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO heartbeat_log (attempted_at, success, status_code, error, response_body)
            VALUES (?, ?, ?, ?, ?)
            """,
            (attempted_at, int(success), status_code, error, response_body),
        )
        conn.execute(
            """
            INSERT INTO agent_state (key, value, updated_at)
            VALUES ('last_heartbeat_success', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("true" if success else "false", attempted_at),
        )
        conn.commit()


def record_claimed_job(path: Path, job: dict[str, object], claimed_at: str) -> None:
    request_json = json.dumps(job.get("request", {}), sort_keys=True)
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, status, request_json, payload_json, created_at, claimed_at, updated_at
            )
            VALUES (?, ?, 'claimed', ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                job_type = excluded.job_type,
                status = excluded.status,
                request_json = excluded.request_json,
                payload_json = excluded.payload_json,
                claimed_at = excluded.claimed_at,
                updated_at = excluded.updated_at
            """,
            (
                str(job["job_id"]),
                str(job["job_type"]),
                request_json,
                request_json,
                claimed_at,
                claimed_at,
                claimed_at,
            ),
        )
        conn.commit()


def record_job_result(
    path: Path,
    job_id: str,
    status: str,
    completed_at: str,
    result: dict[str, object] | None = None,
    error_message: str | None = None,
) -> None:
    result_json = json.dumps(result, sort_keys=True) if result is not None else None
    with connect(path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, result_json = ?, error_message = ?, completed_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (status, result_json, error_message, completed_at, completed_at, job_id),
        )
        conn.commit()


def local_job(path: Path, job_id: str) -> sqlite3.Row | None:
    with connect(path) as conn:
        return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
