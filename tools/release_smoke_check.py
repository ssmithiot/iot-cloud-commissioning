"""Release smoke checker for the IoT Cloud Commissioning API.

Two modes:

--read-only   Safe against ANY environment including production. Checks
              health, environment identity, version, database health,
              schema head, and unauthenticated rejection. Optionally an
              authenticated read if --admin-token is provided. Writes nothing.

--synthetic   Staging/development only (refuses known production hosts).
              Additionally provisions one SMOKE-<run> gateway and runs the
              full round-trip: heartbeat -> job create/claim ->
              device/point/trend-config -> trend upload (twice, idempotency)
              -> trend retrieval.

Exit code 0 = all checks passed; 1 = any check failed; 2 = setup error.
Emits a machine-readable JSON report to stdout (or --output).

Examples:
  python tools/release_smoke_check.py --base-url https://staging.onrender.com \
      --expect-environment staging --admin-token $STG_ADMIN --synthetic

  python tools/release_smoke_check.py --base-url https://iot-cloud-api-dev.onrender.com \
      --expect-environment production --read-only
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from urllib.parse import urlsplit

import requests

# Hosts that must never receive synthetic writes. Keep in sync with
# tools/staging_load_harness.py.
FORBIDDEN_SYNTHETIC_HOSTS = {
    "iot-cloud-api-dev.onrender.com",
}

SYNTHETIC_PREFIX = "SMOKE-"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SystemExit(f"--base-url must be an absolute http(s) URL, got: {base_url!r}")
    return base_url.rstrip("/")


def synthetic_allowed(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host not in FORBIDDEN_SYNTHETIC_HOSTS


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "passed": passed, "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}{': ' + detail if detail and not passed else ''}", file=sys.stderr)
        return passed

    @property
    def all_passed(self) -> bool:
        return all(result["passed"] for result in self.results)


def get_json(session: requests.Session, url: str, timeout: float, **kwargs: object):
    response = session.get(url, timeout=timeout, **kwargs)
    return response, (response.json() if response.headers.get("content-type", "").startswith("application/json") else None)


def run_read_only(args: argparse.Namespace, session: requests.Session, checks: Checks) -> None:
    timeout = args.request_timeout_sec

    response, body = get_json(session, f"{args.base_url}/health", timeout)
    checks.record("health", response.status_code == 200 and isinstance(body, dict) and body.get("status") == "ok", f"status={response.status_code} body={body}")
    if isinstance(body, dict):
        checks.record(
            "environment_identity",
            body.get("environment") == args.expect_environment,
            f"expected {args.expect_environment!r}, got {body.get('environment')!r}",
        )
        checks.record("version_present", bool(body.get("version")), f"body={body}")
        checks.record(
            "health_has_no_secret_shaped_fields",
            set(body) <= {"status", "environment", "version"},
            f"unexpected keys: {sorted(set(body) - {'status', 'environment', 'version'})}",
        )

    response, body = get_json(session, f"{args.base_url}/health/db", timeout)
    checks.record("database_health", response.status_code == 200, f"status={response.status_code}")

    response, body = get_json(session, f"{args.base_url}/health/schema", timeout)
    # Staging/production must be Alembic-managed at head. Local development
    # instances may run with AUTO_CREATE_TABLES=true; accept that only when
    # the expected environment is development.
    schema_ok = (
        response.status_code == 200
        and isinstance(body, dict)
        and (
            (body.get("status") == "ok" and body.get("expected_revisions") == body.get("current_revisions"))
            or (body.get("status") == "development_auto_create" and args.expect_environment == "development")
        )
    )
    checks.record("schema_head", schema_ok, f"body={body}")

    response = session.get(f"{args.base_url}/api/ui/gateways", timeout=timeout)
    checks.record("unauthenticated_rejected", response.status_code == 401, f"status={response.status_code}")

    if args.admin_token:
        response = session.get(
            f"{args.base_url}/api/ui/gateways",
            timeout=timeout,
            headers={"Authorization": f"Bearer {args.admin_token}"},
        )
        checks.record("authenticated_read", response.status_code == 200, f"status={response.status_code}")


def run_synthetic(args: argparse.Namespace, session: requests.Session, checks: Checks) -> None:
    timeout = args.request_timeout_sec
    admin = {"Authorization": f"Bearer {args.admin_token}"}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    gateway_id = f"{SYNTHETIC_PREFIX}{run_id}"
    site_id = f"smoke-site-{run_id}"

    # Provision
    response = session.post(
        f"{args.base_url}/api/admin/gateways/provision",
        timeout=timeout,
        headers=admin,
        json={"gateway_id": gateway_id, "site_id": site_id, "hostname": gateway_id.lower()},
    )
    ok = response.status_code == 200 and isinstance(response.json().get("gateway_api_token"), str)
    if not checks.record("synthetic_provision", ok, f"status={response.status_code}"):
        return
    gateway_headers = {"Authorization": f"Bearer {response.json()['gateway_api_token']}"}

    # Heartbeat
    response = session.post(
        f"{args.base_url}/api/edge/heartbeat",
        timeout=timeout,
        headers=gateway_headers,
        json={
            "gateway_id": gateway_id,
            "site_id": site_id,
            "hostname": gateway_id.lower(),
            "lan_ip": "10.0.0.1",
            "bacnet_port": 47814,
            "agent_version": "0.0.0-smoke",
            "ui_version": "0.0.0-smoke",
            "sqlite_db_ok": True,
            "queued_upload_count": 0,
            "timestamp_utc": utc_iso(),
        },
    )
    checks.record("synthetic_heartbeat", response.status_code == 200, f"status={response.status_code} body={response.text[:200]}")

    # Job round-trip: create, claim exactly once
    response = session.post(
        f"{args.base_url}/api/edge/jobs",
        timeout=timeout,
        headers=admin,
        json={"gateway_id": gateway_id, "job_type": "echo", "request": {"smoke": True}},
    )
    if checks.record("synthetic_job_create", response.status_code == 200, f"status={response.status_code}"):
        job_id = response.json().get("job_id")
        response = session.get(f"{args.base_url}/api/edge/{gateway_id}/jobs/next", timeout=timeout, headers=gateway_headers)
        claimed = response.status_code == 200 and response.json() and response.json().get("job_id") == job_id
        checks.record("synthetic_job_claimed_once", bool(claimed), f"status={response.status_code} body={response.text[:200]}")
        response = session.get(f"{args.base_url}/api/edge/{gateway_id}/jobs/next", timeout=timeout, headers=gateway_headers)
        checks.record("synthetic_job_no_double_claim", response.status_code == 200 and response.json() is None, f"body={response.text[:100]}")

    # Trend round-trip: device -> point -> config -> upload x2 -> retrieve
    response = session.post(
        f"{args.base_url}/api/ui/gateways/{gateway_id}/devices",
        timeout=timeout,
        headers=admin,
        json={"device_instance": 9001, "device_name": "Smoke Device"},
    )
    if not checks.record("synthetic_device_create", response.status_code == 200, f"status={response.status_code} body={response.text[:200]}"):
        return
    device_id = response.json()["id"]

    response = session.post(
        f"{args.base_url}/api/ui/devices/{device_id}/points",
        timeout=timeout,
        headers=admin,
        json={"object_type": "analog-input", "object_instance": 1, "object_name": "Smoke Temp"},
    )
    if not checks.record("synthetic_point_create", response.status_code == 200, f"status={response.status_code} body={response.text[:200]}"):
        return
    point_id = response.json()["id"]

    response = session.put(
        f"{args.base_url}/api/ui/points/{point_id}/trend",
        timeout=timeout,
        headers=admin,
        json={"enabled": True, "interval_sec": 300},
    )
    checks.record("synthetic_trend_config", response.status_code == 200, f"status={response.status_code}")

    # Use a current timestamp: retention pruning (TREND_RETENTION_DAYS) runs
    # on upload and would immediately discard an old fixture date.
    sampled_at = utc_iso()
    sample = [{"point_id": point_id, "sampled_at": sampled_at, "value": "21.5", "quality": "good"}]
    response = session.post(f"{args.base_url}/api/edge/{gateway_id}/trend-samples", timeout=timeout, headers=gateway_headers, json=sample)
    checks.record("synthetic_trend_upload", response.status_code == 200, f"status={response.status_code} body={response.text[:200]}")
    response = session.post(f"{args.base_url}/api/edge/{gateway_id}/trend-samples", timeout=timeout, headers=gateway_headers, json=sample)
    checks.record("synthetic_trend_upload_idempotent", response.status_code == 200 and len(response.json()) == 1, f"status={response.status_code}")

    response = session.get(f"{args.base_url}/api/ui/points/{point_id}/trend?limit=10", timeout=timeout, headers=admin)
    retrieved = response.status_code == 200 and len(response.json()) == 1 and response.json()[0].get("quality") == "good"
    checks.record("synthetic_trend_retrieval", bool(retrieved), f"status={response.status_code} body={response.text[:200]}")

    print(f"NOTE: synthetic rows remain under gateway {gateway_id} / site {site_id}; clean up after review.", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expect-environment", required=True, choices=["development", "staging", "production"])
    parser.add_argument("--admin-token", default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--read-only", action="store_true", help="Safe checks only; writes nothing")
    mode.add_argument("--synthetic", action="store_true", help="Full round-trip with synthetic identities (staging only)")
    parser.add_argument("--request-timeout-sec", type=float, default=30.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    args.base_url = validate_base_url(args.base_url)
    if args.synthetic:
        if not synthetic_allowed(args.base_url):
            parser.error("synthetic mode is refused against known production hosts; use --read-only")
        if not args.admin_token:
            parser.error("--synthetic requires --admin-token")
        if args.expect_environment == "production":
            parser.error("synthetic mode may not target expect-environment=production")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = requests.Session()
    checks = Checks()

    try:
        run_read_only(args, session, checks)
        if args.synthetic and checks.all_passed:
            run_synthetic(args, session, checks)
        elif args.synthetic:
            print("Skipping synthetic phase: read-only checks failed.", file=sys.stderr)
    except requests.RequestException as exc:
        checks.record("transport", False, str(exc))

    report = {
        "tool": "release_smoke_check",
        "base_url": args.base_url,
        "mode": "synthetic" if args.synthetic else "read-only",
        "expected_environment": args.expect_environment,
        "ran_at": utc_iso(),
        "passed": checks.all_passed,
        "checks": checks.results,
    }
    output = json.dumps(report, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    else:
        print(output)
    return 0 if checks.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
