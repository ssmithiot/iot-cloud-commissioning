#!/usr/bin/env python3
"""Bounded read-only API health load test for local or explicitly approved targets."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import json
from statistics import median
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen


SAFE_PATHS = ("/health", "/health/db", "/health/ready")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class RequestResult:
    path: str
    elapsed_ms: float
    status_code: int | None
    error: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API root; defaults to local development")
    parser.add_argument("--requests", type=int, default=100, help="Total requests, distributed across read-only health endpoints")
    parser.add_argument("--concurrency", type=int, default=5, help="Maximum simultaneous requests")
    parser.add_argument("--timeout-sec", type=float, default=10.0, help="Per-request timeout")
    parser.add_argument("--allow-remote", action="store_true", help="Required for any non-local target")
    args = parser.parse_args()
    if args.requests < 1 or args.requests > 100_000:
        parser.error("--requests must be between 1 and 100000")
    if args.concurrency < 1 or args.concurrency > 1_000:
        parser.error("--concurrency must be between 1 and 1000")
    if args.timeout_sec <= 0 or args.timeout_sec > 300:
        parser.error("--timeout-sec must be between 0 and 300")
    return args


def validate_target(base_url: str, allow_remote: bool) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--base-url must be an absolute http(s) URL")
    if parsed.hostname not in LOCAL_HOSTS and not allow_remote:
        raise ValueError("refusing non-local target; pass --allow-remote after confirming it is safe")
    return normalized


def one_request(base_url: str, path: str, timeout_sec: float) -> RequestResult:
    started = time.perf_counter()
    try:
        with urlopen(f"{base_url}{path}", timeout=timeout_sec) as response:  # noqa: S310 - target is explicitly validated above
            response.read()
            status_code = response.status
        return RequestResult(path, (time.perf_counter() - started) * 1000, status_code, None)
    except HTTPError as exc:
        return RequestResult(path, (time.perf_counter() - started) * 1000, exc.code, str(exc))
    except (URLError, TimeoutError, OSError) as exc:
        return RequestResult(path, (time.perf_counter() - started) * 1000, None, str(exc))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return round(ordered[index], 2)


def main() -> int:
    args = parse_args()
    try:
        base_url = validate_target(args.base_url, args.allow_remote)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    paths = [SAFE_PATHS[index % len(SAFE_PATHS)] for index in range(args.requests)]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(args.concurrency, args.requests)) as executor:
        futures = [executor.submit(one_request, base_url, path, args.timeout_sec) for path in paths]
        results = [future.result() for future in as_completed(futures)]
    elapsed = time.perf_counter() - started
    successful = [result for result in results if result.status_code == 200]
    failures = [result for result in results if result.status_code != 200]
    latencies = [result.elapsed_ms for result in successful]
    report = {
        "base_url": base_url,
        "paths": list(SAFE_PATHS),
        "request_count": len(results),
        "concurrency": args.concurrency,
        "successful_count": len(successful),
        "failed_count": len(failures),
        "elapsed_sec": round(elapsed, 2),
        "requests_per_sec": round(len(results) / elapsed, 2) if elapsed else None,
        "latency_ms": {
            "min": round(min(latencies), 2) if latencies else None,
            "p50": round(median(latencies), 2) if latencies else None,
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "failures": [asdict(result) for result in failures[:20]],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
