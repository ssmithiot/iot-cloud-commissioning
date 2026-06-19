from datetime import datetime, timezone
import socket

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import queued_upload_count


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def collect_status(config: AgentConfig, sqlite_db_ok: bool = True) -> dict[str, object]:
    return {
        "gateway_id": config.gateway_id,
        "site_id": config.site_id,
        "hostname": socket.gethostname(),
        "lan_ip": detect_lan_ip(),
        "bacnet_port": config.bacnet_default_port,
        "agent_version": config.agent_version,
        "ui_version": config.ui_version,
        "sqlite_db_ok": sqlite_db_ok,
        "queued_upload_count": queued_upload_count(config.sqlite_path) if sqlite_db_ok else 0,
        "timestamp_utc": utc_timestamp(),
    }

