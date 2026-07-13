from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import socket

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import queued_upload_count, trend_queue_status


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def resource_metrics(sqlite_path: Path) -> dict[str, int | float | None]:
    cpu_count = os.cpu_count() or 1
    try:
        cpu_load_1m = round(os.getloadavg()[0], 2)
        cpu_load_pct = round((cpu_load_1m / cpu_count) * 100, 1)
    except (AttributeError, OSError):
        cpu_load_1m = None
        cpu_load_pct = None

    memory_used_pct = None
    memory_available_mb = None
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        total_kb = values.get("MemTotal")
        available_kb = values.get("MemAvailable")
        if total_kb and available_kb is not None:
            memory_used_pct = round((1 - (available_kb / total_kb)) * 100, 1)
            memory_available_mb = round(available_kb / 1024)
    except (OSError, ValueError):
        pass

    try:
        disk = shutil.disk_usage(sqlite_path.parent)
        disk_used_pct = round((disk.used / disk.total) * 100, 1) if disk.total else None
        disk_free_mb = round(disk.free / (1024 * 1024))
    except OSError:
        disk_used_pct = None
        disk_free_mb = None

    return {
        "cpu_count": cpu_count,
        "cpu_load_1m": cpu_load_1m,
        "cpu_load_pct": cpu_load_pct,
        "memory_used_pct": memory_used_pct,
        "memory_available_mb": memory_available_mb,
        "disk_used_pct": disk_used_pct,
        "disk_free_mb": disk_free_mb,
    }


def collect_status(config: AgentConfig, sqlite_db_ok: bool = True) -> dict[str, object]:
    trend_queue = trend_queue_status(config.sqlite_path) if sqlite_db_ok else {
        "pending_count": 0,
        "deferred_count": 0,
        "oldest_pending_at": None,
        "max_attempt_count": 0,
    }
    return {
        "gateway_id": config.gateway_id,
        "site_id": config.site_id,
        "hostname": socket.gethostname(),
        "lan_ip": detect_lan_ip(),
        "bacnet_port": config.bacnet_default_port,
        "bacnet_router_profile": config.bacnet_router_profile,
        "agent_version": config.agent_version,
        "ui_version": config.ui_version,
        "sqlite_db_ok": sqlite_db_ok,
        "queued_upload_count": queued_upload_count(config.sqlite_path) if sqlite_db_ok else 0,
        "trend_pending_upload_count": trend_queue["pending_count"],
        "trend_deferred_upload_count": trend_queue["deferred_count"],
        "trend_oldest_pending_at": trend_queue["oldest_pending_at"],
        "trend_max_upload_attempt_count": trend_queue["max_attempt_count"],
        **resource_metrics(config.sqlite_path),
        "timestamp_utc": utc_timestamp(),
    }
