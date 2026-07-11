"""point trend configuration and samples

Revision ID: 0010_point_trends
Revises: 0009_gateway_update_requests
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_point_trends"
down_revision = "0009_gateway_update_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "point_trend_configs",
        sa.Column("point_id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_sec", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["point_id"], ["saved_bacnet_points.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("point_id"),
    )
    op.create_index("ix_point_trend_configs_gateway_id", "point_trend_configs", ["gateway_id"], unique=False)
    op.create_table(
        "point_trend_samples",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("point_id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["point_id"], ["saved_bacnet_points.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("point_id", "sampled_at", name="uq_point_trend_sample_time"),
    )
    op.create_index("ix_point_trend_samples_gateway_id", "point_trend_samples", ["gateway_id"], unique=False)
    op.create_index("ix_point_trend_samples_point_id", "point_trend_samples", ["point_id"], unique=False)
    op.create_index("ix_point_trend_samples_sampled_at", "point_trend_samples", ["sampled_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_point_trend_samples_sampled_at", table_name="point_trend_samples")
    op.drop_index("ix_point_trend_samples_point_id", table_name="point_trend_samples")
    op.drop_index("ix_point_trend_samples_gateway_id", table_name="point_trend_samples")
    op.drop_table("point_trend_samples")
    op.drop_index("ix_point_trend_configs_gateway_id", table_name="point_trend_configs")
    op.drop_table("point_trend_configs")
