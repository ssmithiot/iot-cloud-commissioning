import argparse
import logging
import threading
import time
from pathlib import Path

import requests

from iot_cx_agent.config import DEFAULT_CONFIG_PATH, AgentConfig, load_config
from iot_cx_agent.db import initialize_database, record_heartbeat_attempt
from iot_cx_agent.heartbeat import send_heartbeat
from iot_cx_agent.jobs import process_next_job
from iot_cx_agent.status import collect_status, utc_timestamp
from iot_cx_agent.tunnel import run_tunnel_forever
from iot_cx_agent.trends import sample_configured_trends, upload_pending_trend_samples


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("iot-cx-agent")


def run_once(config: AgentConfig) -> bool:
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
        try:
            sample_configured_trends(config)
            upload_pending_trend_samples(config)
        except requests.RequestException as exc:
            logger.warning("Trend sync failed: %s", exc)
        except Exception:
            logger.exception("Trend sampling failed")
        process_next_job(config)
    return heartbeat_success


def safe_record_heartbeat_attempt(config_path: Path, **kwargs: object) -> None:
    try:
        record_heartbeat_attempt(config_path, **kwargs)
    except Exception:
        logger.exception("Failed to record heartbeat attempt locally")


def run_forever(config: AgentConfig) -> None:
    if config.is_provisioned and config.tunnel_enabled:
        threading.Thread(target=run_tunnel_forever, args=(config,), daemon=True).start()
        logger.info("Outbound gateway tunnel enabled for %s", config.gateway_id)
    elif config.tunnel_enabled:
        logger.info("Outbound gateway tunnel skipped until gateway is provisioned")

    while True:
        run_once(config)
        time.sleep(config.heartbeat_interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the IOT Cx edge heartbeat agent.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit.")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.once:
        raise SystemExit(0 if run_once(config) else 1)
    run_forever(config)


if __name__ == "__main__":
    main()
