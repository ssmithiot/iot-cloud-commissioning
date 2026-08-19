import argparse
import hashlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

import requests

from iot_cx_agent.config import DEFAULT_CONFIG_PATH, AgentConfig, load_config
from iot_cx_agent.db import initialize_database, record_heartbeat_attempt
from iot_cx_agent.heartbeat import send_heartbeat
from iot_cx_agent.jobs import process_next_job
from iot_cx_agent.status import collect_status, utc_timestamp
from iot_cx_agent.tunnel import TunnelLeaseWorker
from iot_cx_agent.network_traffic import report as network_traffic_report
from iot_cx_agent.trends import (
    sample_configured_trends,
    sample_local_edge_trends,
    upload_pending_local_trend_samples,
    upload_pending_trend_samples,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("iot-cx-agent")


def startup_stagger_seconds(gateway_id: str) -> int:
    return int.from_bytes(hashlib.sha256(gateway_id.strip().upper().encode()).digest()[:4], "big") % 31


def run_once(config: AgentConfig, tunnel_worker: TunnelLeaseWorker | None = None) -> bool:
    sqlite_db_ok = True
    try:
        initialize_database(config.sqlite_path)
    except OSError:
        sqlite_db_ok = False
        logger.exception("Failed to initialize SQLite database")

    payload = collect_status(config, sqlite_db_ok=sqlite_db_ok)
    attempted_at = utc_timestamp()
    if not config.is_provisioned:
        safe_record_heartbeat_attempt(
            config.sqlite_path,
            attempted_at=attempted_at,
            success=False,
            error="gateway is unprovisioned; heartbeat and job polling skipped",
        )
        logger.warning("Gateway is unprovisioned; heartbeat and job polling skipped")
        return False

    heartbeat_success = False
    try:
        response = send_heartbeat(config, payload)
        heartbeat_success = 200 <= response.status_code < 300
        safe_record_heartbeat_attempt(
            config.sqlite_path,
            attempted_at=attempted_at,
            success=heartbeat_success,
            status_code=response.status_code,
            response_body=response.text[:1000],
        )
        if heartbeat_success:
            logger.info("Heartbeat accepted for gateway %s", config.gateway_id)
        else:
            logger.warning("Heartbeat returned HTTP %s", response.status_code)
    except requests.RequestException as exc:
        safe_record_heartbeat_attempt(config.sqlite_path, attempted_at=attempted_at, success=False, error=str(exc))
        logger.warning("Heartbeat upload failed: %s", exc)

    if sqlite_db_ok:
        # Each trend step is isolated. Local collection is Edge-owned and must
        # keep running when the cloud is unreachable, so a failed upload can
        # never stop sampling, and neither can stop job processing.
        run_step("Local Edge trend sampling", maybe_sample_local_edge_trends, config)
        run_step("Local Edge trend upload", upload_pending_local_trend_samples, config)
        run_step("Cloud trend sampling", sample_configured_trends, config)
        run_step("Cloud trend upload", upload_pending_trend_samples, config)
        process_next_job(config, tunnel_worker.update if tunnel_worker is not None else None)
    return heartbeat_success


def run_step(description: str, step: Callable[[AgentConfig], object], config: AgentConfig) -> None:
    """Run one periodic step, logging and swallowing its failure.

    A gateway is unattended, so a raised exception here must never end the
    agent loop or skip the steps that follow it.
    """
    try:
        step(config)
    except requests.RequestException as exc:
        logger.warning("%s failed: %s", description, exc)
    except Exception:
        logger.exception("%s failed", description)


def maybe_sample_local_edge_trends(config: AgentConfig) -> int:
    if not config.local_edge_trends_enabled:
        logger.debug("Local Edge trend sampling disabled")
        return 0
    return sample_local_edge_trends(config)


def safe_record_heartbeat_attempt(config_path: Path, **kwargs: object) -> None:
    try:
        record_heartbeat_attempt(config_path, **kwargs)
    except Exception:
        logger.exception("Failed to record heartbeat attempt locally")


def run_forever(config: AgentConfig) -> None:
    tunnel_worker = TunnelLeaseWorker(config)
    if config.tunnel_enabled:
        logger.info("Outbound gateway tunnel ready for Cloud lease: %s", config.gateway_id)
    stagger = startup_stagger_seconds(config.gateway_id)
    if stagger:
        logger.info("Applying deterministic %ss startup stagger for %s", stagger, config.gateway_id)
        time.sleep(stagger)

    while True:
        run_once(config, tunnel_worker)
        time.sleep(config.heartbeat_interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the IOT Cx edge heartbeat agent.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit.")
    parser.add_argument("--network-traffic", action="store_true", help="Print local agent network traffic history and exit.")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.network_traffic:
        import json
        print(json.dumps(network_traffic_report(config.sqlite_path), indent=2, sort_keys=True))
        return
    if args.once:
        raise SystemExit(0 if run_once(config) else 1)
    run_forever(config)


if __name__ == "__main__":
    main()
