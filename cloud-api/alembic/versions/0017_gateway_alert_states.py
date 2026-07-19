"""add gateway alert state table for transition-based fleet alerting

Revision ID: 0017_gateway_alert_states
Revises: 0016_scaling_foundations
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_gateway_alert_states"
down_revision = "0016_scaling_foundations"
branch_labels = None
depends_on = None

TABLE_NAME = "gateway_alert_states"


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(TABLE_NAME)


def upgrade() -> None:
    if _has_table():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "gateway_id",
            sa.String(length=120),
            sa.ForeignKey("edge_nodes.gateway_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alert_type", sa.String(length=40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gateway_id", "alert_type", name="uq_gateway_alert_states_gateway_type"),
    )
    op.create_index("ix_gateway_alert_states_gateway_id", TABLE_NAME, ["gateway_id"], unique=False)


def downgrade() -> None:
    if _has_table():
        op.drop_index("ix_gateway_alert_states_gateway_id", table_name=TABLE_NAME)
        op.drop_table(TABLE_NAME)
