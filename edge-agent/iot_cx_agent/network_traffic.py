"""Local-only, application-layer network accounting for iot-cx-agent.

Values are HTTP/WebSocket message bytes known to the agent, not Ethernet/IP/TLS
wire bytes.  No payload contents, URLs, headers, or credentials are stored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from iot_cx_agent.db import record_network_traffic, network_traffic_report

logger = logging.getLogger("iot-cx-agent.network")
CATEGORIES = ("heartbeat", "jobs_poll", "trend_poll", "trend_upload", "job_result", "tunnel_lease", "tunnel_handshake", "tunnel_payload", "other_agent")


def _size(value: object | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8"))


def record(path: Path, category: str, *, tx_bytes: int = 0, rx_bytes: int = 0, success: bool | None = None, tunnel_attempt: bool = False, now: datetime | None = None) -> None:
    try:
        record_network_traffic(path, category, tx_bytes=max(0, tx_bytes), rx_bytes=max(0, rx_bytes), success=success, tunnel_attempt=tunnel_attempt, now=now)
    except Exception as exc:  # Diagnostics must fail open.
        logger.warning("Local network accounting unavailable: %s", exc)


def record_http(path: Path, category: str, response: object | None = None, *, tx_body: object | None = None, success: bool | None = None) -> None:
    request = getattr(response, "request", None)
    tx = _size(getattr(request, "body", None)) or _size(tx_body)
    rx = _size(getattr(response, "content", None))
    if success is None and response is not None:
        status = getattr(response, "status_code", 0)
        success = isinstance(status, int) and 200 <= status < 400
    record(path, category, tx_bytes=tx, rx_bytes=rx, success=success)


def report(path: Path, now: datetime | None = None) -> dict[str, object]:
    return network_traffic_report(path, now=now)
