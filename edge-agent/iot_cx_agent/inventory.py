"""Bounded publisher for the gateway-local Edge UI saved-device inventory."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import connect
from iot_cx_agent.heartbeat import auth_headers


def _snapshot(data_dir: Path, edge_trends_db_path: Path | None = None) -> dict[str, object]:
    devices: list[dict[str, object]] = []
    for path in sorted((data_dir / "devices").glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            device_instance = int(raw["device_id"])
            points = [
                {"object_type": str(point["object_type"]).strip().lower(), "object_instance": int(point["instance"]), "object_name": str(point.get("object_name") or "").strip() or None}
                for point in raw.get("points", [])
                if isinstance(point, dict) and point.get("object_type") and point.get("instance") is not None
            ]
            devices.append({"device_instance": device_instance, "device_name": str(raw.get("device_name") or "").strip() or None, "points": points[:1000]})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    payload: dict[str, object] = {"devices": devices[:250], "trend_snapshot_complete": False, "trend_points": []}
    if edge_trends_db_path is None or not edge_trends_db_path.exists():
        return payload
    try:
        with sqlite3.connect(edge_trends_db_path, timeout=5) as conn:
            rows = conn.execute(
                """SELECT p.device_instance, p.object_type, p.object_instance,
                          MIN(g.interval_sec) AS interval_sec
                   FROM trend_points p JOIN trend_groups g ON g.id=p.group_id
                   WHERE g.enabled=1 GROUP BY p.device_instance, p.object_type, p.object_instance"""
            ).fetchall()
        payload["trend_snapshot_complete"] = True
        payload["trend_points"] = [
            {"device_instance": int(device), "object_type": str(kind).strip().lower(),
             "object_instance": int(instance), "interval_sec": int(interval)}
            for device, kind, instance, interval in rows
        ][:1000]
    except sqlite3.Error:
        # Leave the prior cloud display config alone until the Edge trend DB is
        # readable again; a transient SQLite lock is not an authoritative clear.
        pass
    return payload


def publish_inventory_snapshot(config: AgentConfig) -> bool:
    if config.edge_ui_data_dir is None:
        return False
    now = datetime.now(timezone.utc)
    with connect(config.sqlite_path) as conn:
        row = conn.execute("SELECT value FROM agent_state WHERE key = 'inventory-last-sync'").fetchone()
    if row is not None:
        try:
            previous = datetime.fromisoformat(str(row["value"]).replace("Z", "+00:00"))
            if now - previous < timedelta(seconds=config.inventory_sync_interval_sec):
                return False
        except ValueError:
            pass
    response = requests.put(
        f"{config.cloud_url}/api/edge/{config.gateway_id}/inventory-snapshot",
        headers=auth_headers(config), json=_snapshot(config.edge_ui_data_dir, config.edge_trends_db_path), timeout=30,
    )
    response.raise_for_status()
    with connect(config.sqlite_path) as conn:
        timestamp = now.isoformat()
        conn.execute(
            "INSERT INTO agent_state (key, value, updated_at) VALUES ('inventory-last-sync', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (timestamp, timestamp),
        )
        conn.commit()
    return True
