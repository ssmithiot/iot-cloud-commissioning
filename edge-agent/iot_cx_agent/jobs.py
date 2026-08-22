import logging
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any

import requests

from iot_cx_agent.bacnet import (
    BACNET_RUNTIME_BUSY,
    run_bacnet_discovery,
    run_bacnet_load_points,
    run_bacnet_read,
    run_bacnet_read_bulk,
    run_bacnet_runtime_check,
)
from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import record_claimed_job, record_job_result
from iot_cx_agent.heartbeat import auth_headers
from iot_cx_agent.local_write import dispatch_bacnet_write_batch
from iot_cx_agent.network_traffic import record as record_network
from iot_cx_agent.network_traffic import record_http
from iot_cx_agent.status import utc_timestamp
from iot_cx_agent.trends import (
    TREND_CLOUD_TRANSPORT_SUSPENDED,
    queue_local_trend_backfill,
    trend_cloud_upload_enabled,
    upload_pending_local_trend_samples,
)


logger = logging.getLogger("iot-cx-agent")


def fetch_next_job(config: AgentConfig) -> tuple[dict[str, Any] | None, float | None]:
    try:
        response = requests.get(
            f"{config.cloud_url}/api/edge/{config.gateway_id}/jobs/next",
            headers=auth_headers(config),
            timeout=10,
        )
    except requests.RequestException:
        record_http(config.sqlite_path, "jobs_poll", success=False)
        raise
    record_http(config.sqlite_path, "jobs_poll", response)
    response.raise_for_status()
    lease_expires_at = None
    headers = getattr(response, "headers", {})
    lease_bytes = sum(
        len(str(value).encode())
        for key, value in headers.items()
        if str(key).lower().startswith("x-iot-tunnel-")
    )
    if lease_bytes:
        record_network(config.sqlite_path, "tunnel_lease", rx_bytes=lease_bytes, success=True)
    if headers.get("X-IOT-Tunnel-Requested", "").lower() == "true":
        try:
            lease_expires_at = datetime.fromisoformat(headers["X-IOT-Tunnel-Expires-At"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except (KeyError, ValueError):
            logger.warning("Cloud returned an invalid tunnel lease header; tunnel remains idle")
    return response.json(), lease_expires_at


def post_job_result(
    config: AgentConfig,
    job_id: str,
    status: str,
    result: dict[str, object] | None = None,
    error_message: str | None = None,
) -> requests.Response:
    payload = {"status": status, "result": result, "error_message": error_message}
    try:
        response = requests.post(
            f"{config.cloud_url}/api/edge/jobs/{job_id}/result",
            headers=auth_headers(config),
            json=payload,
            timeout=10,
        )
    except requests.RequestException:
        record_http(config.sqlite_path, "job_result", tx_body=payload, success=False)
        raise
    record_http(config.sqlite_path, "job_result", response, tx_body=payload)
    return response


def execute_job(config: AgentConfig, job: dict[str, Any]) -> tuple[str, dict[str, object] | None, str | None]:
    job_type = str(job["job_type"])
    request = job.get("request", {})

    if job_type == "echo":
        return (
            "completed",
            {
                "echo": True,
                "request": request,
                "gateway_id": config.gateway_id,
                "agent_version": config.agent_version,
            },
            None,
        )

    if job_type == "bacnet_discover":
        result, error_message = run_bacnet_discovery(config, request if isinstance(request, dict) else {})
        if error_message == BACNET_RUNTIME_BUSY:
            return "deferred", result, error_message
        if error_message is not None:
            return "failed", None, error_message
        return "completed", result, None

    if job_type == "bacnet_read":
        result, error_message = run_bacnet_read(config, request if isinstance(request, dict) else {})
        if error_message == BACNET_RUNTIME_BUSY:
            return "deferred", result, error_message
        if error_message is not None:
            return "failed", result, error_message
        return "completed", result, None

    if job_type == "bacnet_read_bulk":
        result, error_message = run_bacnet_read_bulk(config, request if isinstance(request, dict) else {})
        if error_message == BACNET_RUNTIME_BUSY:
            return "deferred", result, error_message
        if error_message is not None:
            return "failed", result, error_message
        return "completed", result, None

    if job_type == "bacnet_load_points":
        result, error_message = run_bacnet_load_points(config, request if isinstance(request, dict) else {})
        if error_message == BACNET_RUNTIME_BUSY:
            return "deferred", result, error_message
        if error_message is not None:
            return "failed", result, error_message
        return "completed", result, None

    if job_type == "bacnet_write_batch":
        result, error_message = dispatch_bacnet_write_batch(config, job)
        if error_message is not None:
            return "failed", result, error_message
        return "completed", result, None

    if job_type == "bacnet_runtime_check":
        result, error_message = run_bacnet_runtime_check(config, request if isinstance(request, dict) else {})
        if error_message is not None:
            return "failed", result, error_message
        return "completed", result, None

    if job_type == "trend_sync_now":
        if not trend_cloud_upload_enabled():
            return "completed", {"status": "suspended", "message": TREND_CLOUD_TRANSPORT_SUSPENDED, "uploaded_samples": 0}, None
        request = request if isinstance(request, dict) else {}
        try:
            max_batches = max(1, min(5, int(request.get("max_batches", 1))))
        except (TypeError, ValueError):
            return "failed", None, "max_batches must be an integer between 1 and 5"
        uploaded = 0
        for _ in range(max_batches):
            count = upload_pending_local_trend_samples(config, force=True)
            uploaded += count
            if count < config.trend_local_upload_batch_size:
                break
        return "completed", {"uploaded_samples": uploaded, "max_batches": max_batches}, None

    if job_type == "trend_backfill":
        if not trend_cloud_upload_enabled():
            return "completed", {"status": "suspended", "message": TREND_CLOUD_TRANSPORT_SUSPENDED, "queued_samples": 0, "uploaded_samples": 0}, None
        request = request if isinstance(request, dict) else {}
        since = str(request.get("since") or "")
        until = str(request.get("until") or "")
        try:
            start = datetime.fromisoformat(since.replace("Z", "+00:00"))
            end = datetime.fromisoformat(until.replace("Z", "+00:00"))
            max_samples = max(1, min(1000, int(request.get("max_samples", 500))))
        except (TypeError, ValueError):
            return "failed", None, "since, until and max_samples must be valid bounded values"
        if start > end or end - start > timedelta(days=31):
            return "failed", None, "backfill range must be ordered and no longer than 31 days"
        queued = queue_local_trend_backfill(config, start.isoformat(), end.isoformat(), limit=max_samples)
        uploaded = upload_pending_local_trend_samples(config, force=True) if queued else 0
        return "completed", {"queued_samples": queued, "uploaded_samples": uploaded, "max_samples": max_samples}, None

    return "failed", None, f"Unknown job_type: {job_type}"


def process_next_job(config: AgentConfig, tunnel_lease_consumer: Callable[[float | None], None] | None = None) -> bool:
    try:
        job, lease_expires_at = fetch_next_job(config)
    except requests.RequestException as exc:
        logger.warning("Job poll failed: %s", exc)
        return False

    if tunnel_lease_consumer is not None:
        tunnel_lease_consumer(lease_expires_at)

    if job is None:
        return True

    job_id = str(job["job_id"])
    claimed_at = utc_timestamp()
    record_claimed_job(config.sqlite_path, job, claimed_at)

    status, result, error_message = execute_job(config, job)
    completed_at = utc_timestamp()
    record_job_result(config.sqlite_path, job_id, status, completed_at, result=result, error_message=error_message)

    try:
        response = post_job_result(config, job_id, status, result=result, error_message=error_message)
        response.raise_for_status()
        logger.info("Job %s reported as %s", job_id, status)
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to post result for job %s: %s", job_id, exc)
        return False
