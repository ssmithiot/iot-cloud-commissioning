"""Add cloud storage for Edge-owned local trend samples.

Revision ID: 0022_edge_local_trend_samples
Revises: 0021_gateway_ui_only_updates
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_edge_local_trend_samples"
down_revision: str | Sequence[str] | None = "0021_gateway_ui_only_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("edge_local_trend_samples"):
        op.create_table(
            "edge_local_trend_samples",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("gateway_id", sa.String(length=120), nullable=False),
            sa.Column("group_name", sa.String(length=255), nullable=False),
            sa.Column("device_instance", sa.Integer(), nullable=False),
            sa.Column("object_type", sa.String(length=80), nullable=False),
            sa.Column("object_instance", sa.Integer(), nullable=False),
            sa.Column("object_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("value_text", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("read_source", sa.String(length=80), nullable=True),
            sa.Column("error_text", sa.String(length=1000), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", name="uq_edge_local_trend_sample_event"),
        )
        inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("edge_local_trend_samples")}
    if "ix_edge_local_trend_samples_gateway_id" not in indexes:
        op.create_index("ix_edge_local_trend_samples_gateway_id", "edge_local_trend_samples", ["gateway_id"], unique=False)
    if "ix_edge_local_trend_samples_sampled_at" not in indexes:
        op.create_index("ix_edge_local_trend_samples_sampled_at", "edge_local_trend_samples", ["sampled_at"], unique=False)
    if "ix_edge_local_trend_samples_gateway_sampled" not in indexes:
        op.create_index("ix_edge_local_trend_samples_gateway_sampled", "edge_local_trend_samples", ["gateway_id", "sampled_at"], unique=False)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("edge_local_trend_samples"):
        op.drop_table("edge_local_trend_samples")
