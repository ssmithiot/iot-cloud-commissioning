from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://iot-cloud-api-dev.onrender.com"
DEFAULT_GATEWAY_ID = "GW777"
BACNET_PORT = 47814


class SmokeFailure(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        normalized = raw_value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def request_json(
    method: str,
    base_url: str,
    path: str,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except URLError as exc:
        raise SmokeFailure(f"HTTP request failed for {method} {path}: {exc}") from exc


def require_status(status: int, expected: int, label: str, body: Any) -> None:
    if status != expected:
        raise SmokeFailure(f"{label} returned HTTP {status}, expected {expected}: {body}")


def find_gateway(gateways: list[dict[str, Any]], gateway_id: str) -> dict[str, Any]:
    for gateway in gateways:
        if gateway.get("gateway_id") == gateway_id:
            return gateway
    raise SmokeFailure(f"{gateway_id} was not found in authenticated gateway list")


def find_job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any]:
    for job in jobs:
        if job.get("job_id") == job_id:
            return job
    raise SmokeFailure(f"{job_id} was not found in job list")


def summarize_gateway(gateway: dict[str, Any]) -> str:
    heartbeat = _parse_timestamp(gateway.get("latest_heartbeat_at"))
    age = "unknown"
    if heartbeat is not None:
        age = f"{int((_utc_now() - heartbeat).total_seconds())}s"
    return (
        f"{gateway.get('gateway_id')} status={gateway.get('latest_status')} "
        f"bacnet_port={gateway.get('bacnet_port')} heartbeat_age={age}"
    )


def create_runtime_check_job(base_url: str, token: str, gateway_id: str) -> str:
    request_body = {
        "gateway_id": gateway_id,
        "job_type": "bacnet_runtime_check",
        "request": {"bacnet_port": BACNET_PORT},
    }
    status, body = request_json("POST", base_url, "/api/edge/jobs", token=token, body=request_body)
    require_status(status, 200, "POST /api/edge/jobs", body)
    if body.get("request_json") != {"bacnet_port": BACNET_PORT}:
        raise SmokeFailure(f"created job request_json mismatch: {body.get('request_json')}")
    if body.get("status") != "queued":
        raise SmokeFailure(f"created job status was {body.get('status')!r}, expected 'queued'")
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise SmokeFailure(f"created job did not include a job_id: {body}")
    print(f"queued {job_id} for {gateway_id} with request {{'bacnet_port': {BACNET_PORT}}}")
    return job_id


def poll_job(base_url: str, token: str, job_id: str, timeout_sec: int, poll_sec: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_job: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        status, jobs = request_json("GET", base_url, "/api/edge/jobs", token=token)
        require_status(status, 200, "GET /api/edge/jobs", jobs)
        if not isinstance(jobs, list):
            raise SmokeFailure(f"GET /api/edge/jobs returned non-list body: {jobs}")
        last_job = find_job(jobs, job_id)
        job_status = last_job.get("status")
        print(f"{job_id} status={job_status}")
        if job_status == "completed":
            return last_job
        if job_status == "failed":
            raise SmokeFailure(f"{job_id} failed: {last_job.get('error_message')} result={last_job.get('result_json')}")
        time.sleep(poll_sec)
    raise SmokeFailure(f"{job_id} did not complete before timeout; last job state={last_job}")


def run_smoke(args: argparse.Namespace) -> None:
    token = os.environ.get("IOT_ADMIN_API_TOKEN", "").strip()
    if not token:
        raise SmokeFailure("IOT_ADMIN_API_TOKEN is not set in this process")

    status, body = request_json("GET", args.base_url, "/health")
    require_status(status, 200, "GET /health", body)
    print("health ok")

    status, body = request_json("GET", args.base_url, "/health/db")
    require_status(status, 200, "GET /health/db", body)
    print("database health ok")

    status, body = request_json("GET", args.base_url, "/api/edge/gateways")
    require_status(status, 401, "GET /api/edge/gateways without token", body)
    print("missing admin auth returns 401")

    status, gateways = request_json("GET", args.base_url, "/api/edge/gateways", token=token)
    require_status(status, 200, "GET /api/edge/gateways with admin token", gateways)
    if not isinstance(gateways, list):
        raise SmokeFailure(f"gateway list returned non-list body: {gateways}")
    gateway = find_gateway(gateways, args.gateway_id)
    if gateway.get("bacnet_port") != BACNET_PORT:
        raise SmokeFailure(f"{args.gateway_id} bacnet_port={gateway.get('bacnet_port')}, expected {BACNET_PORT}")
    print(f"gateway ok: {summarize_gateway(gateway)}")

    job_id = args.job_id or create_runtime_check_job(args.base_url, token, args.gateway_id)
    completed = poll_job(args.base_url, token, job_id, args.timeout_sec, args.poll_sec)
    result = completed.get("result_json")
    if not isinstance(result, dict):
        raise SmokeFailure(f"{job_id} completed without result_json object: {completed}")
    if result.get("bacnet_port") != BACNET_PORT:
        raise SmokeFailure(f"{job_id} result bacnet_port={result.get('bacnet_port')}, expected {BACNET_PORT}")
    print(f"completed {job_id} with bacnet_port={result.get('bacnet_port')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MVP-012 live admin/gateway smoke against the deployed cloud API.")
    parser.add_argument("--base-url", default=os.environ.get("IOT_CLOUD_API_URL", DEFAULT_BASE_URL))
    parser.add_argument("--gateway-id", default=os.environ.get("IOT_SMOKE_GATEWAY_ID", DEFAULT_GATEWAY_ID))
    parser.add_argument("--job-id", default=os.environ.get("IOT_SMOKE_JOB_ID"))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("IOT_SMOKE_TIMEOUT_SEC", "180")))
    parser.add_argument("--poll-sec", type=int, default=int(os.environ.get("IOT_SMOKE_POLL_SEC", "5")))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        run_smoke(args)
    except SmokeFailure as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
