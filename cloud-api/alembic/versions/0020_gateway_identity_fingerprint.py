"""add gateway physical identity fingerprint fields

Revision ID: 0020_gateway_identity_fingerprint
Revises: 0019_uuid_schema_alignment
Create Date: 2026-07-16 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0020_gateway_identity_fingerprint"
down_revision: str | Sequence[str] | None = "0019_uuid_schema_alignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    for table_name in ("edge_nodes", "edge_heartbeats"):
        columns = _columns(table_name)
        if "machine_id" not in columns:
            op.add_column(table_name, sa.Column("machine_id", sa.String(length=128), nullable=True))
        if "primary_mac" not in columns:
            op.add_column(table_name, sa.Column("primary_mac", sa.String(length=64), nullable=True))


def downgrade() -> None:
    for table_name in ("edge_heartbeats", "edge_nodes"):
        columns = _columns(table_name)
        if "primary_mac" in columns:
            op.drop_column(table_name, "primary_mac")
        if "machine_id" in columns:
            op.drop_column(table_name, "machine_id")
