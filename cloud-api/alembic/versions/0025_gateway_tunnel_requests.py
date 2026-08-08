"""Add durable operator-authorized gateway tunnel requests.

Revision ID: 0025_gateway_tunnel_requests
Revises: 0024_update_target_commits
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0025_gateway_tunnel_requests"
down_revision: str | Sequence[str] | None = "0024_update_target_commits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "gateway_tunnel_requests" not in inspector.get_table_names():
        op.create_table(
            "gateway_tunnel_requests",
            sa.Column("gateway_id", sa.String(length=120), sa.ForeignKey("edge_nodes.gateway_id"), primary_key=True),
            sa.Column("requested_duration_minutes", sa.Integer(), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("requested_by", sa.String(length=255), nullable=True),
            sa.Column("state", sa.String(length=20), nullable=False, server_default="closed"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_gateway_tunnel_requests_active", "gateway_tunnel_requests", ["state", "expires_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "gateway_tunnel_requests" in inspector.get_table_names():
        op.drop_index("ix_gateway_tunnel_requests_active", table_name="gateway_tunnel_requests")
        op.drop_table("gateway_tunnel_requests")
