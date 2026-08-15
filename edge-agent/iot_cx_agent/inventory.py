"""Read-only Edge Live Device inventory transport; never invokes BACnet."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import connect
from iot_cx_agent.heartbeat import auth_headers


def _timestamp(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _last_known(rows: object, object_type: str, instance: int) -> dict[str, object] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or str(row.get("object_type", "")).strip().lower() != object_type:
            continue
        try:
            if int(row.get("instance")) != instance:
                continue
        except (TypeError, ValueError):
            continue
        priority = row.get("active_priority")
        try:
            priority = int(priority) if str(priority or "").strip() else None
        except (TypeError, ValueError):
            priority = None
        return {
            "display_value": _timestamp(row.get("display_present_value") or row.get("present_value")),
            "raw_value": _timestamp(row.get("raw_present_value")),
            "active_priority": priority,
            "priority_array": _timestamp(row.get("priority_array")),
            "read_status": _timestamp(row.get("status")),
            "read_source": _timestamp(row.get("read_source")),
            "source_timestamp": _timestamp(row.get("timestamp")),
        }
    return None


def inventory_snapshot(data_dir: Path | None) -> dict[str, object]:
    devices: list[dict[str, object]] = []
    if data_dir is not None:
        for path in sorted((data_dir / "devices").glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                instance = int(raw.get("device_id"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            profile_id = str(raw.get("device_profile_id") or path.stem).strip()
            if not profile_id:
                continue
            points: list[dict[str, object]] = []
            seen: set[tuple[str, int]] = set()
            for point in raw.get("points", []) if isinstance(raw.get("points"), list) else []:
                if not isinstance(point, dict):
                    continue
                object_type = str(point.get("object_type", "")).strip().lower()
                try:
                    object_instance = int(point.get("instance"))
                except (TypeError, ValueError):
                    continue
                if not object_type or object_instance < 0 or (object_type, object_instance) in seen:
                    continue
                seen.add((object_type, object_instance))
                item: dict[str, object] = {"object_type": object_type, "object_instance": object_instance, "property_name": "present-value", "object_name": str(point.get("object_name") or "") or None}
                last = _last_known(raw.get("last_rows"), object_type, object_instance)
                if last is not None:
                    item["last_known"] = last
                points.append(item)
            metadata = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
            devices.append({"edge_device_profile_id": profile_id, "device_instance": instance, "device_name": str(raw.get("device_name") or "") or None, "metadata": metadata, "created_at": _timestamp(raw.get("created_at")), "updated_at": _timestamp(raw.get("updated_at")), "last_refreshed_at": _timestamp(raw.get("last_refreshed_at")), "points": sorted(points, key=lambda item: (str(item["object_type"]), int(item["object_instance"])))})
    devices.sort(key=lambda item: str(item["edge_device_profile_id"]))
    canonical = {"complete_snapshot": True, "devices": devices}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"inventory_hash": digest, **canonical}


def sync_inventory(config: AgentConfig) -> bool:
    snapshot = inventory_snapshot(config.edge_ui_data_dir)
    now = datetime.now(timezone.utc)
    with connect(config.sqlite_path) as conn:
        row = conn.execute("SELECT value FROM agent_state WHERE key='inventory-last-success-hash'").fetchone()
        previous = str(row["value"]) if row else ""
        pending = conn.execute("SELECT id FROM sync_queue WHERE item_type='inventory_snapshot' AND status='pending' ORDER BY id DESC LIMIT 1").fetchone()
        if snapshot["inventory_hash"] != previous or pending is not None:
            conn.execute("DELETE FROM sync_queue WHERE item_type='inventory_snapshot' AND status='pending'")
            conn.execute("INSERT INTO sync_queue (item_type,payload_json,status,created_at,updated_at) VALUES ('inventory_snapshot',?,'pending',?,?)", (json.dumps(snapshot, sort_keys=True), now.isoformat(), now.isoformat()))
            conn.commit()
        queued = conn.execute("SELECT id,payload_json FROM sync_queue WHERE item_type='inventory_snapshot' AND status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id DESC LIMIT 1", (now.isoformat(),)).fetchone()
    if queued is None:
        return False
    try:
        response = requests.put(f"{config.cloud_url}/api/edge/{config.gateway_id}/inventory", headers=auth_headers(config), json=json.loads(str(queued["payload_json"])), timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        with connect(config.sqlite_path) as conn:
            conn.execute("UPDATE sync_queue SET attempt_count=attempt_count+1,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?", (str(exc)[:1000], (now + timedelta(seconds=30)).isoformat(), now.isoformat(), int(queued["id"])))
            conn.commit()
        raise
    with connect(config.sqlite_path) as conn:
        conn.execute("UPDATE sync_queue SET status='uploaded',updated_at=?,last_error=NULL,next_attempt_at=NULL WHERE id=?", (now.isoformat(), int(queued["id"])))
        conn.execute("INSERT INTO agent_state (key,value,updated_at) VALUES ('inventory-last-success-hash',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(snapshot["inventory_hash"]), now.isoformat()))
        conn.commit()
    return True
