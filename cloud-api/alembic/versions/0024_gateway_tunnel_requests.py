"""Persist explicit, short-lived Cloud tunnel requests.

Revision ID: 0024_gateway_tunnel_requests
Revises: 0023_edge_release_targets
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0024_gateway_tunnel_requests"
down_revision: str | Sequence[str] | None = "0023_edge_release_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "gateway_tunnel_requests" in inspector.get_table_names():
        return
    op.create_table(
        "gateway_tunnel_requests",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("gateway_id", sa.String(length=120), sa.ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("requested_by", sa.String(length=320), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gateway_tunnel_requests_expires_at", "gateway_tunnel_requests", ["expires_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "gateway_tunnel_requests" in inspector.get_table_names():
        op.drop_table("gateway_tunnel_requests")
