"""Gateway-local trend sampler.

Configuration and samples live in the Edge UI's independent SQLite database.
This deliberately does not change cloud trend configuration or upload local
pilot samples.  It uses the normal agent BACnet runtime lock and never changes
the Live Devices/RPM implementation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
from typing import Any

from iot_cx_agent.bacnet import BACNET_RUNTIME_BUSY, run_bacnet_read_bulk
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.status import network_counters, resource_metrics

LOCAL_TREND_RPM_BATCH_SIZE = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
        conn.executemany(
            """INSERT INTO trend_samples (trend_point_id, sampled_at, value_text, status, read_source, error_text)
            VALUES (?, ?, ?, ?, ?, ?)""",
            [(sample["trend_point_id"], sample["sampled_at"], sample.get("value_text"), sample["status"], sample.get("read_source"), sample.get("error_text")) for sample in samples],
        )


def sample_local_edge_trends(config: AgentConfig) -> int:
    """Sample any due Edge-owned groups and persist them locally.

    A missing/uninitialized UI database is intentionally a no-op. This lets an
    agent package be safely rolled out before the feature is enabled.
    """
    if not config.local_edge_trends_enabled or not config.edge_trends_db_path.exists():
        return 0
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
