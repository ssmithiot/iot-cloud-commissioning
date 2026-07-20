"""Gateway-local trend sampler.

Configuration and samples live in the Edge UI's independent SQLite database.
This deliberately does not change cloud trend configuration or upload local
pilot samples.  It uses the normal agent BACnet runtime lock and never changes
the Live Devices/RPM implementation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time
from typing import Any
from uuid import uuid4

import requests

from iot_cx_agent.bacnet import BACNET_RUNTIME_BUSY, run_bacnet_read_bulk
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.heartbeat import auth_headers
from iot_cx_agent.status import network_counters, resource_metrics

LOCAL_TREND_RPM_BATCH_SIZE = 20

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS trend_upload_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    trend_sample_id INTEGER NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'uploaded')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    uploaded_at TEXT,
    FOREIGN KEY(trend_sample_id) REFERENCES trend_samples(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS trend_sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trend_upload_outbox_pending ON trend_upload_outbox(state, next_attempt_at, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_outbox_schema(path: Path) -> None:
    with _connect(path) as conn:
        conn.executescript(OUTBOX_SCHEMA)


def _backfill_unqueued_samples(path: Path) -> None:
    """Seed one durable event for samples captured before the outbox upgrade."""
    now = _now()
    with _connect(path) as conn:
        rows = conn.execute(
            """SELECT s.id FROM trend_samples s
               LEFT JOIN trend_upload_outbox o ON o.trend_sample_id=s.id
               WHERE o.id IS NULL ORDER BY s.id"""
        ).fetchall()
        for row in rows:
            conn.execute(
                """INSERT INTO trend_upload_outbox
                (event_id, trend_sample_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)""",
                (str(uuid4()), int(row["id"]), now, now),
            )


def _due_groups(path: Path, now: str) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT g.* FROM trend_groups g
            WHERE g.enabled = 1 AND (
                (SELECT MAX(r.started_at) FROM trend_runs r WHERE r.group_id = g.id) IS NULL
                OR (julianday(?) - julianday((SELECT MAX(r.started_at) FROM trend_runs r WHERE r.group_id = g.id))) * 86400 >= g.interval_sec
            )
            ORDER BY g.id
            """,
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def _points(path: Path, group_id: int) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trend_points WHERE group_id = ? ORDER BY device_instance, object_type, object_instance",
            (group_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _start_run(path: Path, group_id: int, started_at: str, requested_count: int, metrics: dict[str, Any], network: dict[str, int | None]) -> int:
    with _connect(path) as conn:
        cursor = conn.execute(
            """INSERT INTO trend_runs (group_id, started_at, requested_count, cpu_load_pct, memory_used_pct, network_rx_bytes, network_tx_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (group_id, started_at, requested_count, metrics.get("cpu_load_pct"), metrics.get("memory_used_pct"), network.get("rx_bytes"), network.get("tx_bytes")),
        )
        return int(cursor.lastrowid)


def _finish_run(path: Path, run_id: int, *, returned: int, deferred: int, duration_ms: int, error: str | None) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE trend_runs SET completed_at=?, returned_count=?, deferred_count=?, duration_ms=?, error_text=? WHERE id=?",
            (_now(), returned, deferred, duration_ms, error, run_id),
        )


def _record_samples(path: Path, samples: list[dict[str, Any]]) -> None:
    if not samples:
        return
    with _connect(path) as conn:
        for sample in samples:
            cursor = conn.execute(
                """INSERT INTO trend_samples (trend_point_id, sampled_at, value_text, status, read_source, error_text)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (sample["trend_point_id"], sample["sampled_at"], sample.get("value_text"), sample["status"], sample.get("read_source"), sample.get("error_text")),
            )
            # The outbox write is deliberately in the same SQLite transaction.
            # A recorded local sample therefore always has a durable cloud-copy
            # event, even if power or network fails immediately afterward.
            conn.execute(
                """INSERT INTO trend_upload_outbox
                (event_id, trend_sample_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)""",
                (str(uuid4()), int(cursor.lastrowid), sample["sampled_at"], sample["sampled_at"]),
            )


def _state_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM trend_sync_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_state(conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
    conn.execute(
        """INSERT INTO trend_sync_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, now),
    )


def _pending_outbox(path: Path, now: str, limit: int) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            """SELECT o.id AS outbox_id, o.event_id, o.attempt_count, s.sampled_at,
                      s.value_text, s.status, s.read_source, s.error_text,
                      g.name AS group_name, p.device_instance, p.object_type,
                      p.object_instance, p.object_name
               FROM trend_upload_outbox o
               JOIN trend_samples s ON s.id=o.trend_sample_id
               JOIN trend_points p ON p.id=s.trend_point_id
               JOIN trend_groups g ON g.id=p.group_id
               WHERE o.state='pending' AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)
               ORDER BY o.id LIMIT ?""",
            (now, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _mark_uploaded(path: Path, outbox_ids: list[int], now: str) -> None:
    if not outbox_ids:
        return
    marks = ",".join("?" for _ in outbox_ids)
    with _connect(path) as conn:
        conn.execute(
            f"UPDATE trend_upload_outbox SET state='uploaded', uploaded_at=?, updated_at=?, last_error=NULL, next_attempt_at=NULL WHERE id IN ({marks})",
            [now, now, *outbox_ids],
        )
        _set_state(conn, "last_upload_attempt_at", now, now)


def _record_upload_failure(path: Path, outbox_ids: list[int], error: str, retry_at: str, now: str) -> None:
    if not outbox_ids:
        return
    marks = ",".join("?" for _ in outbox_ids)
    with _connect(path) as conn:
        conn.execute(
            f"UPDATE trend_upload_outbox SET attempt_count=attempt_count+1, last_error=?, next_attempt_at=?, updated_at=? WHERE id IN ({marks})",
            [error[:1000], retry_at, now, *outbox_ids],
        )
        _set_state(conn, "last_upload_attempt_at", now, now)


def _upload_interval_elapsed(path: Path, now: datetime, interval_sec: int) -> bool:
    with _connect(path) as conn:
        previous = _state_value(conn, "last_upload_attempt_at")
    if not previous:
        return True
    try:
        prior = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - prior).total_seconds() >= interval_sec


def upload_local_edge_trend_samples(config: AgentConfig) -> int:
    """Copy acknowledged batches from the Edge-owned outbox to the cloud.

    This never touches the legacy cloud-trend queue. The cloud deduplicates on
    event_id, so retrying after a connection interruption is safe.
    """
    if not config.local_edge_trends_enabled or not config.local_edge_trend_cloud_sync_enabled or not config.edge_trends_db_path.exists():
        return 0
    _ensure_outbox_schema(config.edge_trends_db_path)
    _backfill_unqueued_samples(config.edge_trends_db_path)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    if not _upload_interval_elapsed(config.edge_trends_db_path, now_dt, config.local_edge_trend_upload_interval_sec):
        return 0
    rows = _pending_outbox(config.edge_trends_db_path, now, config.local_edge_trend_upload_batch_size)
    if not rows:
        return 0
    payload = [{
        "event_id": row["event_id"], "group_name": row["group_name"],
        "device_instance": row["device_instance"], "object_type": row["object_type"],
        "object_instance": row["object_instance"], "object_name": row["object_name"],
        "sampled_at": row["sampled_at"], "value_text": row["value_text"],
        "status": row["status"], "read_source": row["read_source"], "error_text": row["error_text"],
    } for row in rows]
    ids = [int(row["outbox_id"]) for row in rows]
    try:
        response = requests.post(
            f"{config.cloud_url}/api/edge/{config.gateway_id}/local-trend-samples",
            headers=auth_headers(config), json=payload, timeout=30,
        )
        response.raise_for_status()
        accepted = set(response.json().get("accepted_event_ids", []))
        expected = {str(row["event_id"]) for row in rows}
        if accepted != expected:
            raise RuntimeError("Cloud did not acknowledge every local trend event in the batch")
    except Exception as exc:
        highest_attempt = max(int(row["attempt_count"]) for row in rows)
        retry_seconds = min(config.local_edge_trend_upload_retry_max_sec, config.local_edge_trend_upload_retry_base_sec * (2 ** min(6, highest_attempt)))
        _record_upload_failure(config.edge_trends_db_path, ids, str(exc), (now_dt + timedelta(seconds=retry_seconds)).isoformat(timespec="seconds"), now)
        raise
    _mark_uploaded(config.edge_trends_db_path, ids, now)
    return len(rows)


def sample_local_edge_trends(config: AgentConfig) -> int:
    """Sample any due Edge-owned groups and persist them locally.

    A missing/uninitialized UI database is intentionally a no-op. This lets an
    agent package be safely rolled out before the feature is enabled.
    """
    if not config.local_edge_trends_enabled or not config.edge_trends_db_path.exists():
        return 0
    _ensure_outbox_schema(config.edge_trends_db_path)
    now = _now()
    try:
        groups = _due_groups(config.edge_trends_db_path, now)
    except sqlite3.Error:
        return 0
    completed = 0
    for group in groups:
        points = _points(config.edge_trends_db_path, int(group["id"]))
        if not points:
            continue
        started = time.monotonic()
        run_id = _start_run(config.edge_trends_db_path, int(group["id"]), now, len(points), resource_metrics(config.sqlite_path), network_counters())
        returned = 0
        deferred = 0
        errors: list[str] = []
        samples: list[dict[str, Any]] = []
        by_device: dict[int, list[dict[str, Any]]] = {}
        for point in points:
            by_device.setdefault(int(point["device_instance"]), []).append(point)
        for device_instance, device_points in by_device.items():
            for start in range(0, len(device_points), LOCAL_TREND_RPM_BATCH_SIZE):
                chunk = device_points[start:start + LOCAL_TREND_RPM_BATCH_SIZE]
                request_points = [{"saved_point_id": str(point["id"]), "object_type": point["object_type"], "object_instance": point["object_instance"]} for point in chunk]
                result, error = run_bacnet_read_bulk(config, {"device_instance": device_instance, "points": request_points})
                if error == BACNET_RUNTIME_BUSY:
                    deferred += len(chunk)
                    continue
                if error:
                    errors.append(error)
                values = result.get("values", []) if isinstance(result, dict) else []
                values_by_id = {str(value.get("saved_point_id")): value for value in values if isinstance(value, dict)}
                for point in chunk:
                    value = values_by_id.get(str(point["id"]), {})
                    status = str(value.get("status") or "missing")
                    if status == "ok":
                        returned += 1
                    samples.append({
                        "trend_point_id": int(point["id"]), "sampled_at": now,
                        "value_text": None if value.get("value") is None else str(value.get("value")),
                        "status": status, "read_source": value.get("read_source"),
                        "error_text": value.get("error") or error,
                    })
        _record_samples(config.edge_trends_db_path, samples)
        _finish_run(config.edge_trends_db_path, run_id, returned=returned, deferred=deferred, duration_ms=round((time.monotonic() - started) * 1000), error="; ".join(dict.fromkeys(errors)) or None)
        completed += 1
    return completed
