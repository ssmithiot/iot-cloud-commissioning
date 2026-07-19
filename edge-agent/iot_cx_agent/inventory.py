"""Bounded publisher for the gateway-local Edge UI saved-device inventory."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import connect
from iot_cx_agent.heartbeat import auth_headers


def _snapshot(data_dir: Path) -> dict[str, object]:
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
    return {"devices": devices[:250]}


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
        headers=auth_headers(config), json=_snapshot(config.edge_ui_data_dir), timeout=30,
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
