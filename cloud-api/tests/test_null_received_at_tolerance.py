"""2026-07-13 deploy incident regression: historical production rows carry
NULL received_at (their tables predate strict governance, so the NOT NULL
constraint in the models never applied there). Response schemas must
tolerate None; migration 0018 backfills the data.

The tolerance is tested at the schema layer because test databases are
created from the models and correctly enforce NOT NULL — the production
defect state cannot be inserted here, which is exactly why it escaped.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

EDGE_AGENT_PATH = Path(__file__).resolve().parents[2] / "edge-agent"
if str(EDGE_AGENT_PATH) not in sys.path:
    sys.path.append(str(EDGE_AGENT_PATH))

os.environ["DATABASE_URL"] = "sqlite:///./test-cloud-api.db"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["GATEWAY_AUTH_PEPPER"] = "test-pepper"
os.environ["IOT_ADMIN_API_TOKEN"] = "test-admin-token"
os.environ["SUPABASE_JWT_SECRET"] = "test-supabase-jwt-secret"

from app.schemas import GatewayHeartbeatTrendOut, PointTrendSampleOut


def test_heartbeat_trend_out_tolerates_null_received_at() -> None:
    item = GatewayHeartbeatTrendOut(
        timestamp_utc=datetime.now(timezone.utc),
        received_at=None,  # historical production rows
        status="online",
        sqlite_db_ok=True,
        queued_upload_count=0,
        agent_version="0.1.0",
        ui_version="0.1.0",
    )
    assert item.received_at is None
    # And still accepts real values.
    assert GatewayHeartbeatTrendOut(
        timestamp_utc=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        status="online",
        sqlite_db_ok=True,
        queued_upload_count=0,
        agent_version="0.1.0",
        ui_version="0.1.0",
    ).received_at is not None


def test_point_trend_sample_out_tolerates_null_received_at() -> None:
    item = PointTrendSampleOut(
        point_id="p-1",
        sampled_at=datetime.now(timezone.utc),
        value="21.5",
        gateway_id="GW001",
        source="edge-agent",
        received_at=None,  # historical production rows
    )
    assert item.received_at is None


def test_omitted_received_at_defaults_to_none() -> None:
    # from_attributes construction with a row object lacking the value.
    item = GatewayHeartbeatTrendOut(
        timestamp_utc=datetime.now(timezone.utc),
        status="degraded",
        sqlite_db_ok=False,
        queued_upload_count=3,
        agent_version="0.1.0",
        ui_version="0.1.0",
    )
    assert item.received_at is None
