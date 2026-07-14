"""Automated staging release gate for the IoT Cloud Commissioning API.

A CI-oriented safety gate, distinct from the manual `release_smoke_check.py`
tool. This one is entirely environment-variable driven (no CLI target can be
passed by accident), refuses to run against anything that looks like
production, requires the target to self-report as `staging` with a
current Alembic schema, exercises the core admin/gateway path with a single
SMOKE- prefixed synthetic gateway, and cleans up the synthetic credentials it
created before exiting.

Required environment variables:
    STAGING_BASE_URL     Absolute http(s) URL of the staging deployment.
    STAGING_ADMIN_TOKEN  Admin API token for that staging deployment.

Optional environment variables:
    STAGING_GATE_EXTRA_PRODUCTION_FINGERPRINTS
        Comma-separated substrings (hosts, project refs, etc.) that, if
        found in STAGING_BASE_URL, cause the gate to refuse to run. Merged
        with the built-in denylist; never replaces it.
    STAGING_GATE_REQUEST_TIMEOUT_SEC
        Per-request timeout in seconds (default 30).
    STAGING_GATE_SKIP_CLEANUP
        "true" to leave the synthetic gateway's credentials active for
        post-mortem debugging (default "false" — clean up).
    STAGING_GATE_REPORT_PATH
        If set, the JSON report is also written to this path.

Exit codes:
    0  All checks passed.
    1  One or more checks failed.
    2  Configuration/safety error (missing env vars, refused production URL)
       — no request was ever sent.

Example:
    export STAGING_BASE_URL=https://iot-cloud-api-staging.onrender.com
    export STAGING_ADMIN_TOKEN=...
    python tools/staging_release_gate.py

This tool never accepts a base URL or token as a command-line argument on
purpose: CI secrets belong in environment variables, not in argv (which can
leak into process listings and job logs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import requests

# Hosts that must never receive synthetic gate traffic. Keep in sync with
# tools/release_smoke_check.py FORBIDDEN_SYNTHETIC_HOSTS and
# tools/staging_load_harness.py FORBIDDEN_HOSTS.
PRODUCTION_HOST_DENYLIST = frozenset({"iot-cloud-api-dev.onrender.com"})

SYNTHETIC_PREFIX = "SMOKE-"

REQUIRED_ENVIRONMENT = "staging"


class GateConfigurationError(Exception):
    """Raised for missing configuration or a refused (unsafe) target.

    Always maps to exit code 2: the gate never sent a request.
    """


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def production_fingerprints(extra_fingerprints: str | None) -> frozenset[str]:
    """Merge the built-in production denylist with operator-supplied extras.

    Extras are additive only — there is no way to shrink the built-in
    denylist via configuration.
    """
    fingerprints = set(PRODUCTION_HOST_DENYLIST)
    if extra_fingerprints:
        fingerprints.update(
            fragment.strip().lower() for fragment in extra_fingerprints.split(",") if fragment.strip()
        )
    return frozenset(fingerprints)


def validate_staging_url(base_url: str, extra_fingerprints: str | None = None) -> str:
    """Validate STAGING_BASE_URL and refuse anything production-shaped.

    Refuses if the hostname exactly matches a known production host, or if
    any production fingerprint substring appears anywhere in the host or
    full URL (catches subdomain tricks like `evil.iot-cloud-api-dev.onrender.com`
    and lookalike paths). Raises GateConfigurationError on refusal; never
    makes a network call.
    """
    if not base_url:
        raise GateConfigurationError("STAGING_BASE_URL is required and must not be empty")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise GateConfigurationError(f"STAGING_BASE_URL must be an absolute http(s) URL, got: {base_url!r}")

    host = parts.hostname.lower()
    haystack = base_url.lower()
    fingerprints = production_fingerprints(extra_fingerprints)
    matched = sorted(fp for fp in fingerprints if fp in host or fp in haystack)
    if matched:
        raise GateConfigurationError(
            "REFUSED: STAGING_BASE_URL matches known production host/fingerprint(s) "
            f"{matched}. This gate only targets staging and will not run."
        )
    return base_url.rstrip("/")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GateChecks:
    results: list[CheckResult] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append(CheckResult(name, passed, detail))
        marker = "PASS" if passed else "FAIL"
        suffix = f": {detail}" if detail and not passed else ""
        print(f"[{marker}] {name}{suffix}", file=sys.stderr)
        return passed

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(result.passed for result in self.results)

    def as_list(self) -> list[dict[str, object]]:
        return [{"check": r.name, "passed": r.passed, "detail": r.detail} for r in self.results]


def exit_code_for(checks: GateChecks) -> int:
    """Pure pass/fail -> exit-code decision. No configuration errors reach
    here (those exit 2 before any check runs); an empty result set (nothing
    ran) is treated as a failure, not a silent pass."""
    return 0 if checks.all_passed else 1


def preflight_passed(checks: GateChecks) -> bool:
    """Whether it is safe to proceed to the synthetic provisioning phase.

    Identical to `checks.all_passed` today, but kept as a separate function
    (rather than inlined) because it is the single gate that must never be
    bypassed: provisioning must not run against a target that failed health,
    environment-identity, schema, or auth checks.
    """
    return checks.all_passed


@dataclass
class GateConfig:
    base_url: str
    admin_token: str
    request_timeout_sec: float
    extra_fingerprints: str | None
    skip_cleanup: bool
    report_path: str | None


def load_config(env: dict[str, str] | None = None) -> GateConfig:
    env = os.environ if env is None else env
    base_url_raw = env.get("STAGING_BASE_URL", "").strip()
    admin_token = env.get("STAGING_ADMIN_TOKEN", "").strip()

    missing = [name for name, value in (("STAGING_BASE_URL", base_url_raw), ("STAGING_ADMIN_TOKEN", admin_token)) if not value]
    if missing:
        raise GateConfigurationError(f"Missing required environment variable(s): {', '.join(missing)}")

    extra_fingerprints = env.get("STAGING_GATE_EXTRA_PRODUCTION_FINGERPRINTS")
    base_url = validate_staging_url(base_url_raw, extra_fingerprints)

    try:
        timeout = float(env.get("STAGING_GATE_REQUEST_TIMEOUT_SEC", "30"))
    except ValueError as exc:
        raise GateConfigurationError("STAGING_GATE_REQUEST_TIMEOUT_SEC must be numeric") from exc

    skip_cleanup = env.get("STAGING_GATE_SKIP_CLEANUP", "false").strip().lower() in {"1", "true", "yes"}
    report_path = env.get("STAGING_GATE_REPORT_PATH") or None

    return GateConfig(
        base_url=base_url,
        admin_token=admin_token,
        request_timeout_sec=timeout,
        extra_fingerprints=extra_fingerprints,
        skip_cleanup=skip_cleanup,
        report_path=report_path,
    )


def get_json(session: requests.Session, url: str, timeout: float, **kwargs: object):
    response = session.get(url, timeout=timeout, **kwargs)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
    return response, body


def run_preflight(config: GateConfig, session: requests.Session, checks: GateChecks) -> None:
    """Read-only checks that must all pass before any synthetic write.
    Safe against any target, including production, by design — but the URL
    refusal in load_config() means production is never reached anyway."""
    timeout = config.request_timeout_sec

    response, body = get_json(session, f"{config.base_url}/health", timeout)
    checks.record("health_ok", response.status_code == 200 and isinstance(body, dict) and body.get("status") == "ok", f"status={response.status_code} body={body}")
    reported_environment = body.get("environment") if isinstance(body, dict) else None
    checks.record(
        "environment_is_staging",
        reported_environment == REQUIRED_ENVIRONMENT,
        f"expected {REQUIRED_ENVIRONMENT!r}, got {reported_environment!r}",
    )

    response, body = get_json(session, f"{config.base_url}/health/db", timeout)
    checks.record("database_health", response.status_code == 200 and isinstance(body, dict) and body.get("status") == "ok", f"status={response.status_code} body={body}")

    response, body = get_json(session, f"{config.base_url}/health/schema", timeout)
    schema_current = (
        response.status_code == 200
        and isinstance(body, dict)
        and body.get("status") == "ok"
        and bool(body.get("expected_revisions"))
        and body.get("expected_revisions") == body.get("current_revisions")
    )
    checks.record("schema_current", schema_current, f"body={body}")

    response = session.get(f"{config.base_url}/api/ui/gateways", timeout=timeout)
    checks.record("unauthenticated_request_rejected", response.status_code == 401, f"status={response.status_code}")

    response = session.get(
        f"{config.base_url}/api/ui/gateways",
        timeout=timeout,
        headers={"Authorization": f"Bearer {config.admin_token}"},
    )
    checks.record("admin_token_authenticates", response.status_code == 200, f"status={response.status_code}")


@dataclass
class SyntheticGateway:
    gateway_id: str
    site_id: str
    gateway_token: str | None = None


def run_synthetic_gateway_path(
    config: GateConfig,
    session: requests.Session,
    checks: GateChecks,
    gateway: SyntheticGateway,
) -> None:
    """Provision one SMOKE- gateway and verify the core admin/gateway path:
    provision -> heartbeat -> admin visibility -> job create/claim once."""
    timeout = config.request_timeout_sec
    admin_headers = {"Authorization": f"Bearer {config.admin_token}"}

    response = session.post(
        f"{config.base_url}/api/admin/gateways/provision",
        timeout=timeout,
        headers=admin_headers,
        json={"gateway_id": gateway.gateway_id, "site_id": gateway.site_id, "hostname": gateway.gateway_id.lower()},
    )
    provisioned = response.status_code == 200 and isinstance(response.json().get("gateway_api_token"), str)
    if not checks.record("synthetic_gateway_provisioned", provisioned, f"status={response.status_code} body={response.text[:200]}"):
        return
    gateway.gateway_token = response.json()["gateway_api_token"]
    gateway_headers = {"Authorization": f"Bearer {gateway.gateway_token}"}

    response = session.post(
        f"{config.base_url}/api/edge/heartbeat",
        timeout=timeout,
        headers=gateway_headers,
        json={
            "gateway_id": gateway.gateway_id,
            "site_id": gateway.site_id,
            "hostname": gateway.gateway_id.lower(),
            "lan_ip": "10.0.0.1",
            "bacnet_port": 47814,
            "agent_version": "0.0.0-staginggate",
            "ui_version": "0.0.0-staginggate",
            "sqlite_db_ok": True,
            "queued_upload_count": 0,
            "timestamp_utc": utc_iso(),
        },
    )
    checks.record("synthetic_heartbeat_accepted", response.status_code == 200, f"status={response.status_code} body={response.text[:200]}")

    response = session.get(f"{config.base_url}/api/ui/gateways/{gateway.gateway_id}", timeout=timeout, headers=admin_headers)
    visible = response.status_code == 200 and isinstance(response.json(), dict) and response.json().get("gateway_id") == gateway.gateway_id
    checks.record("synthetic_gateway_visible_to_admin", visible, f"status={response.status_code}")

    response = session.post(
        f"{config.base_url}/api/edge/jobs",
        timeout=timeout,
        headers=admin_headers,
        json={"gateway_id": gateway.gateway_id, "job_type": "echo", "request": {"staging_gate": True}},
    )
    if not checks.record("synthetic_job_created", response.status_code == 200, f"status={response.status_code} body={response.text[:200]}"):
        return
    job_id = response.json().get("job_id")

    response = session.get(f"{config.base_url}/api/edge/{gateway.gateway_id}/jobs/next", timeout=timeout, headers=gateway_headers)
    claimed_correct_job = response.status_code == 200 and response.json() and response.json().get("job_id") == job_id
    checks.record("synthetic_job_claimed_once", bool(claimed_correct_job), f"status={response.status_code} body={response.text[:200]}")

    response = session.get(f"{config.base_url}/api/edge/{gateway.gateway_id}/jobs/next", timeout=timeout, headers=gateway_headers)
    checks.record("synthetic_job_no_double_claim", response.status_code == 200 and response.json() is None, f"status={response.status_code} body={response.text[:100]}")


def cleanup_synthetic_gateway(
    config: GateConfig,
    session: requests.Session,
    checks: GateChecks,
    gateway: SyntheticGateway,
) -> None:
    """Revoke every credential created for the synthetic gateway. This is
    the only cleanup the API surface supports: there is no gateway/site
    deprovision endpoint, so the EdgeNode and Site rows for SMOKE- gateways
    remain (identifiable by the SMOKE- prefix) for periodic manual/staging
    housekeeping — this is a known, documented limitation, not a bug."""
    timeout = config.request_timeout_sec
    admin_headers = {"Authorization": f"Bearer {config.admin_token}"}

    response = session.get(
        f"{config.base_url}/api/admin/gateways/{gateway.gateway_id}/credentials",
        timeout=timeout,
        headers=admin_headers,
    )
    if response.status_code != 200:
        checks.record("cleanup_credentials_revoked", False, f"could not list credentials: status={response.status_code}")
        return

    credentials = response.json()
    active = [c for c in credentials if c.get("status") != "revoked"]
    all_revoked = True
    for credential in active:
        revoke_response = session.post(
            f"{config.base_url}/api/admin/credentials/{credential['credential_id']}/revoke",
            timeout=timeout,
            headers=admin_headers,
        )
        if revoke_response.status_code != 200:
            all_revoked = False
    checks.record("cleanup_credentials_revoked", all_revoked, f"{len(active)} credential(s) revoked for {gateway.gateway_id}")


def build_synthetic_gateway() -> SyntheticGateway:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
    return SyntheticGateway(gateway_id=f"{SYNTHETIC_PREFIX}{run_id}", site_id=f"{SYNTHETIC_PREFIX}SITE-{run_id}")


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - no CLI args by design
    try:
        config = load_config()
    except GateConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        print(json.dumps({"tool": "staging_release_gate", "passed": False, "configuration_error": str(exc)}, indent=2))
        return 2

    session = requests.Session()
    checks = GateChecks()
    gateway: SyntheticGateway | None = None

    try:
        run_preflight(config, session, checks)
        if preflight_passed(checks):
            gateway = build_synthetic_gateway()
            try:
                run_synthetic_gateway_path(config, session, checks, gateway)
            finally:
                if gateway.gateway_token is not None and not config.skip_cleanup:
                    cleanup_synthetic_gateway(config, session, checks, gateway)
        else:
            print("Skipping synthetic gateway phase: preflight checks failed.", file=sys.stderr)
    except requests.RequestException as exc:
        checks.record("transport", False, str(exc))

    passed = checks.all_passed
    report = {
        "tool": "staging_release_gate",
        "base_url": config.base_url,
        "required_environment": REQUIRED_ENVIRONMENT,
        "synthetic_gateway_id": gateway.gateway_id if gateway else None,
        "ran_at": utc_iso(),
        "passed": passed,
        "checks": checks.as_list(),
    }
    output = json.dumps(report, indent=2)
    if config.report_path:
        with open(config.report_path, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    print(output)
    print(f"STAGING RELEASE GATE: {'PASS' if passed else 'FAIL'} ({sum(r.passed for r in checks.results)}/{len(checks.results)} checks)", file=sys.stderr)
    return exit_code_for(checks)


if __name__ == "__main__":
    raise SystemExit(main())
