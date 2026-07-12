import ipaddress
from typing import Any
from urllib.parse import urlsplit

import requests

from iot_cx_agent.config import AgentConfig


WRITE_BATCH_PATH = "/api/internal/edge-agent/bacnet/write-batch"
WRITE_RESPONSE_STATUSES = {"ok", "partial", "error"}


def local_write_base_url(config: AgentConfig) -> str:
    configured = config.local_ui_url.rstrip("/")
    parsed = urlsplit(configured)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Local edge UI write target must use an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Local edge UI write target must be a plain loopback base URL")

    hostname = parsed.hostname.lower()
    if hostname != "localhost":
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("Local edge UI write target must be loopback-only")
    return configured


def dispatch_bacnet_write_batch(
    config: AgentConfig,
    job: dict[str, Any],
) -> tuple[dict[str, object] | None, str | None]:
    token = (config.edge_agent_write_token or "").strip()
    if not token:
        return None, "EDGE_AGENT_WRITE_TOKEN is not configured"
    if config.local_ui_write_timeout_sec <= 0:
        return None, "local_ui_write_timeout_sec must be greater than zero"

    request_payload = job.get("request")
    if not isinstance(request_payload, dict):
        return None, "BACnet write job request must be an object"

    try:
        base_url = local_write_base_url(config)
    except ValueError as exc:
        return None, str(exc)

    payload = dict(request_payload)
    payload["job_id"] = str(job.get("job_id") or "")
    try:
        response = requests.post(
            f"{base_url}{WRITE_BATCH_PATH}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=config.local_ui_write_timeout_sec,
        )
    except requests.RequestException as exc:
        return None, f"Local edge UI write adapter request failed ({type(exc).__name__})"

    if response.status_code != 200:
        return None, f"Local edge UI write adapter returned HTTP {response.status_code}"

    try:
        result = response.json()
    except ValueError:
        return None, "Local edge UI write adapter returned invalid JSON"
    if not isinstance(result, dict):
        return None, "Local edge UI write adapter returned a non-object response"
    if result.get("job_type") != "bacnet_write_batch":
        return None, "Local edge UI write adapter returned an unexpected job_type"
    if result.get("status") not in WRITE_RESPONSE_STATUSES:
        return None, "Local edge UI write adapter returned an invalid status"
    if not isinstance(result.get("results"), list):
        return None, "Local edge UI write adapter returned invalid command results"
    return result, None
