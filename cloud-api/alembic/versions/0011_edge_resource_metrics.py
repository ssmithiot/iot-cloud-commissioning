"""store edge resource metrics in heartbeats

Revision ID: 0011_edge_resource_metrics
Revises: 0010_point_trends
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_edge_resource_metrics"
down_revision = "0010_point_trends"
branch_labels = None
depends_on = None


RESOURCE_COLUMNS = (
    ("cpu_count", sa.Integer()),
    ("cpu_load_1m", sa.Float()),
    ("cpu_load_pct", sa.Float()),
    ("memory_used_pct", sa.Float()),
    ("memory_available_mb", sa.Integer()),
    ("disk_used_pct", sa.Float()),
    ("disk_free_mb", sa.Integer()),
)


def upgrade() -> None:
    for table_name in ("edge_nodes", "edge_heartbeats"):
        for column_name, column_type in RESOURCE_COLUMNS:
            op.add_column(table_name, sa.Column(column_name, column_type, nullable=True))


def downgrade() -> None:
    for table_name in ("edge_heartbeats", "edge_nodes"):
        for column_name, _ in reversed(RESOURCE_COLUMNS):
            op.drop_column(table_name, column_name)
