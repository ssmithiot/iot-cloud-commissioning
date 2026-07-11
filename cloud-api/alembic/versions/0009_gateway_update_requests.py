"""gateway update request queue

Revision ID: 0009_gateway_update_requests
Revises: 0008_site_coordinates
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_gateway_update_requests"
down_revision = "0008_site_coordinates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gateway_update_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("requested_by", sa.String(length=320), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gateway_update_requests_gateway_id", "gateway_update_requests", ["gateway_id"], unique=False)
    op.create_index("ix_gateway_update_requests_status", "gateway_update_requests", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_gateway_update_requests_status", table_name="gateway_update_requests")
    op.drop_index("ix_gateway_update_requests_gateway_id", table_name="gateway_update_requests")
    op.drop_table("gateway_update_requests")
