"""Staging-only synthetic load harness for the IoT Cloud Commissioning API.

Simulates a fleet of synthetic gateways (heartbeats, job polling, trend
uploads) and concurrent operator reads against a STAGING deployment.

Safety model:
- An explicit --base-url is required; there is no default target.
- Known production hosts are refused unconditionally.
- --confirm-staging is required before any traffic is sent.
- All gateway IDs are prefixed LOADTEST- so synthetic rows are identifiable
  and removable.
- Uses a dedicated staging admin token and staging gateway credentials only.
- Levels are small by default; nothing runs automatically.

Example (Level 1 functional concurrency):
    python tools/staging_load_harness.py \
        --base-url https://<staging-service>.onrender.com \
        --confirm-staging \
        --admin-token $STAGING_ADMIN_TOKEN \
        --gateways 5 --duration-sec 60 --heartbeat-interval-sec 10 \
        --output results-level1.json

The harness reports request counts, latency percentiles, and error rates as
machine-readable JSON. It never deletes or mutates non-LOADTEST data.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import random
import statistics
import sys
import threading
import time
from urllib.parse import urlsplit

import requests

# Hosts that must never receive synthetic load. Extend as production
# deployments are added.
FORBIDDEN_HOSTS = {
    "iot-cloud-api-dev.onrender.com",
}

SYNTHETIC_GATEWAY_PREFIX = "LOADTEST-"
SYNTHETIC_SITE_PREFIX = "loadtest-site-"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SystemExit(f"--base-url must be an absolute http(s) URL, got: {base_url!r}")
    host = parts.hostname.lower()
    if host in FORBIDDEN_HOSTS:
        raise SystemExit(
            f"REFUSED: {host} is a known production host. "
            "This harness only targets staging deployments."
        )
    return base_url.rstrip("/")


@dataclass
class Metrics:
    lock: threading.Lock = field(default_factory=threading.Lock)
    latencies_ms: dict[str, list[float]] = field(default_factory=dict)
    statuses: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)

    def record(self, operation: str, latency_ms: float | None, status: int | None, error: str | None) -> None:
        with self.lock:
            if latency_ms is not None:
                self.latencies_ms.setdefault(operation, []).append(latency_ms)
            if status is not None:
                bucket = self.statuses.setdefault(operation, {})
                key = str(status)
                bucket[key] = bucket.get(key, 0) + 1
            if error is not None:
                self.errors[operation] = self.errors.get(operation, 0) + 1

    def summary(self) -> dict[str, object]:
        def pct(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            ordered = sorted(values)
            index = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
            return round(ordered[index], 1)

        with self.lock:
            operations: dict[str, object] = {}
            for operation, values in sorted(self.latencies_ms.items()):
                operations[operation] = {
                    "count": len(values),
                    "latency_ms_p50": pct(values, 0.50),
                    "latency_ms_p95": pct(values, 0.95),
                    "latency_ms_p99": pct(values, 0.99),
                    "latency_ms_max": round(max(values), 1) if values else 0.0,
                    "latency_ms_mean": round(statistics.fmean(values), 1) if values else 0.0,
                    "statuses": dict(sorted(self.statuses.get(operation, {}).items())),
                    "transport_errors": self.errors.get(operation, 0),
                }
            for operation, count in self.errors.items():
                if operation not in operations:
                    operations[operation] = {"count": 0, "transport_errors": count}
            return operations


def timed_request(
    metrics: Metrics,
    operation: str,
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float,
    **kwargs: object,
) -> requests.Response | None:
    start = time.perf_counter()
    try:
        response = session.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        metrics.record(operation, None, None, str(exc))
        return None
    metrics.record(operation, (time.perf_counter() - start) * 1000, response.status_code, None)
    return response


def heartbeat_payload(gateway_id: str, site_id: str) -> dict[str, object]:
    return {
        "gateway_id": gateway_id,
        "site_id": site_id,
        "hostname": gateway_id.lower(),
        "lan_ip": "10.0.0.1",
        "bacnet_port": 47814,
        "agent_version": "0.0.0-loadtest",
        "ui_version": "0.0.0-loadtest",
        "sqlite_db_ok": True,
        "queued_upload_count": 0,
        "trend_pending_upload_count": random.randint(0, 5),
        "trend_deferred_upload_count": 0,
        "trend_max_upload_attempt_count": 0,
        "cpu_load_pct": round(random.uniform(1, 60), 1),
        "memory_used_pct": round(random.uniform(10, 80), 1),
        "disk_used_pct": round(random.uniform(10, 70), 1),
        "timestamp_utc": utc_iso(),
    }


def provision_synthetic_gateway(
    args: argparse.Namespace,
    metrics: Metrics,
    session: requests.Session,
    index: int,
) -> tuple[str, str, str] | None:
    """Provision one synthetic gateway; returns (gateway_id, site_id, token)."""
    gateway_id = f"{SYNTHETIC_GATEWAY_PREFIX}{args.run_id}-{index:04d}"
    site_id = f"{SYNTHETIC_SITE_PREFIX}{args.run_id}"
    response = timed_request(
        metrics,
        "provision",
        session,
        "POST",
        f"{args.base_url}/api/admin/gateways/provision",
        timeout=args.request_timeout_sec,
        headers={"Authorization": f"Bearer {args.admin_token}"},
        json={"gateway_id": gateway_id, "site_id": site_id, "hostname": gateway_id.lower()},
    )
    if response is None or response.status_code >= 400:
        detail = "" if response is None else response.text[:300]
        print(f"provision failed for {gateway_id}: {detail}", file=sys.stderr)
        return None
    token = response.json().get("gateway_api_token")
    if not isinstance(token, str):
        print(f"provision for {gateway_id} returned no token", file=sys.stderr)
        return None
    return gateway_id, site_id, token


def gateway_worker(
    args: argparse.Namespace,
    metrics: Metrics,
    stop_event: threading.Event,
    gateway_id: str,
    site_id: str,
    token: str,
) -> None:
    session = requests.Session()
    headers = {"Authorization": f"Bearer {token}"}
    while not stop_event.is_set():
        cycle_start = time.monotonic()
        timed_request(
            metrics,
            "heartbeat",
            session,
            "POST",
            f"{args.base_url}/api/edge/heartbeat",
            timeout=args.request_timeout_sec,
            headers=headers,
            json=heartbeat_payload(gateway_id, site_id),
        )
        timed_request(
            metrics,
            "job_poll",
            session,
            "GET",
            f"{args.base_url}/api/edge/{gateway_id}/jobs/next",
            timeout=args.request_timeout_sec,
            headers=headers,
        )
        if args.trend_batch_size > 0:
            timed_request(
                metrics,
                "trend_config_poll",
                session,
                "GET",
                f"{args.base_url}/api/edge/{gateway_id}/trend-configs",
                timeout=args.request_timeout_sec,
                headers=headers,
            )
        elapsed = time.monotonic() - cycle_start
        stop_event.wait(max(0.0, args.heartbeat_interval_sec - elapsed))


def operator_worker(
    args: argparse.Namespace,
    metrics: Metrics,
    stop_event: threading.Event,
) -> None:
    session = requests.Session()
    headers = {"Authorization": f"Bearer {args.admin_token}"}
    while not stop_event.is_set():
        timed_request(
            metrics,
            "ui_gateway_list",
            session,
            "GET",
            f"{args.base_url}/api/ui/gateways",
            timeout=args.request_timeout_sec,
            headers=headers,
        )
        timed_request(
            metrics,
            "ui_gateway_summary",
            session,
            "GET",
            f"{args.base_url}/api/ui/gateways/summary",
            timeout=args.request_timeout_sec,
            headers=headers,
        )
        stop_event.wait(args.operator_interval_sec)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="Staging base URL (required; production hosts refused)")
    parser.add_argument("--confirm-staging", action="store_true", help="Required acknowledgement that the target is staging")
    parser.add_argument("--admin-token", required=True, help="STAGING admin API token")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"), help="Identifier embedded in synthetic gateway IDs")
    parser.add_argument("--gateways", type=int, default=5, help="Synthetic gateway count (default 5)")
    parser.add_argument("--operators", type=int, default=1, help="Concurrent synthetic operator readers (default 1)")
    parser.add_argument("--duration-sec", type=int, default=60, help="Test duration in seconds (default 60)")
    parser.add_argument("--heartbeat-interval-sec", type=float, default=10.0, help="Per-gateway cycle interval (default 10)")
    parser.add_argument("--operator-interval-sec", type=float, default=5.0, help="Operator read interval (default 5)")
    parser.add_argument("--trend-batch-size", type=int, default=0, help="Reserved for trend upload simulation once staging points exist (default 0 = off)")
    parser.add_argument("--request-timeout-sec", type=float, default=30.0, help="Per-request timeout (default 30)")
    parser.add_argument("--output", default=None, help="Write JSON results to this file")
    args = parser.parse_args(argv)

    args.base_url = validate_base_url(args.base_url)
    if not args.confirm_staging:
        parser.error("--confirm-staging is required: this harness must only target staging")
    if args.gateways < 1 or args.gateways > 2000:
        parser.error("--gateways must be between 1 and 2000")
    if args.duration_sec < 1 or args.duration_sec > 24 * 3600:
        parser.error("--duration-sec must be between 1 and 86400")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    metrics = Metrics()
    admin_session = requests.Session()

    print(f"Provisioning {args.gateways} synthetic gateways (run {args.run_id}) against {args.base_url}")
    provisioned: list[tuple[str, str, str]] = []
    for index in range(args.gateways):
        result = provision_synthetic_gateway(args, metrics, admin_session, index)
        if result is not None:
            provisioned.append(result)
    if not provisioned:
        print("No gateways provisioned; aborting.", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    started_at = utc_iso()
    start_monotonic = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(provisioned) + args.operators) as pool:
        for gateway_id, site_id, token in provisioned:
            pool.submit(gateway_worker, args, metrics, stop_event, gateway_id, site_id, token)
        for _ in range(args.operators):
            pool.submit(operator_worker, args, metrics, stop_event)
        try:
            time.sleep(args.duration_sec)
        except KeyboardInterrupt:
            print("Interrupted; stopping workers.", file=sys.stderr)
        stop_event.set()

    wall_seconds = round(time.monotonic() - start_monotonic, 1)
    results = {
        "harness": "staging_load_harness",
        "base_url": args.base_url,
        "run_id": args.run_id,
        "started_at": started_at,
        "finished_at": utc_iso(),
        "wall_seconds": wall_seconds,
        "gateways_requested": args.gateways,
        "gateways_provisioned": len(provisioned),
        "operators": args.operators,
        "heartbeat_interval_sec": args.heartbeat_interval_sec,
        "operations": metrics.summary(),
        "cleanup_note": (
            f"Synthetic rows use gateway prefix {SYNTHETIC_GATEWAY_PREFIX}{args.run_id} "
            f"and site {SYNTHETIC_SITE_PREFIX}{args.run_id}; remove them from staging after review."
        ),
    }
    output = json.dumps(results, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
        print(f"Results written to {args.output}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
