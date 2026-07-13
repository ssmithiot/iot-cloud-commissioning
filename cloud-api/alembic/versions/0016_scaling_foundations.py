"""add hot-path composite indexes for job polling and heartbeat history

Revision ID: 0016_scaling_foundations
Revises: 0015_trend_hardening
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_scaling_foundations"
down_revision = "0015_trend_hardening"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_edge_jobs_gateway_status_created", "edge_jobs", ["gateway_id", "status", "created_at"]),
    ("ix_edge_heartbeats_gateway_timestamp", "edge_heartbeats", ["gateway_id", "timestamp_utc"]),
)


def _existing_indexes(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    for index_name, table_name, columns in INDEXES:
        if index_name not in _existing_indexes(table_name):
            op.create_index(index_name, table_name, columns, unique=False)


def downgrade() -> None:
    for index_name, table_name, _ in reversed(INDEXES):
        if index_name in _existing_indexes(table_name):
            op.drop_index(index_name, table_name=table_name)
