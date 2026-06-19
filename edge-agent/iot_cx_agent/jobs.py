import logging
from typing import Any

import requests

from iot_cx_agent.config import AgentConfig
from iot_cx_agent.db import record_claimed_job, record_job_result
from iot_cx_agent.status import utc_timestamp


logger = logging.getLogger("iot-cx-agent")


def fetch_next_job(config: AgentConfig) -> dict[str, Any] | None:
    response = requests.get(
        f"{config.cloud_url}/api/edge/{config.gateway_id}/jobs/next",
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def post_job_result(
    config: AgentConfig,
    job_id: str,
    status: str,
    result: dict[str, object] | None = None,
    error_message: str | None = None,
) -> requests.Response:
    return requests.post(
        f"{config.cloud_url}/api/edge/jobs/{job_id}/result",
        json={"status": status, "result": result, "error_message": error_message},
        timeout=10,
    )


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

    return "failed", None, f"Unknown job_type: {job_type}"


def process_next_job(config: AgentConfig) -> bool:
    try:
        job = fetch_next_job(config)
    except requests.RequestException as exc:
        logger.warning("Job poll failed: %s", exc)
        return False

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
