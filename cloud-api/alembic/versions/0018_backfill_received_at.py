"""backfill NULL received_at on historical heartbeat and trend sample rows

Production rows written before the received_at columns were governed carry
NULL, which broke strict response schemas (2026-07-13 deploy incident).

Revision ID: 0018_backfill_received_at
Revises: 0017_gateway_alert_states
"""

from alembic import op


revision = "0018_backfill_received_at"
down_revision = "0017_gateway_alert_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE edge_heartbeats SET received_at = timestamp_utc WHERE received_at IS NULL")
    op.execute("UPDATE point_trend_samples SET received_at = created_at WHERE received_at IS NULL")


def downgrade() -> None:
    # Backfill only; nothing to reverse.
    pass
