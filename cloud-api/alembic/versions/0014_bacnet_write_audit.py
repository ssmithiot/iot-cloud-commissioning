"""BACnet write audit and point command state

Revision ID: 0014_bacnet_write_audit
Revises: 0013_inventory_lifecycle
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_bacnet_write_audit"
down_revision = "0013_inventory_lifecycle"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    point_columns = _columns("saved_bacnet_points")
    additions = (
        ("active_priority", sa.Integer()),
        ("priority_array", sa.String(length=2000)),
        ("relinquish_default", sa.String(length=255)),
        ("state_text", sa.String(length=2000)),
    )
    for name, column_type in additions:
        if name not in point_columns:
            op.add_column("saved_bacnet_points", sa.Column(name, column_type, nullable=True))

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("bacnet_write_batches"):
        op.create_table(
            "bacnet_write_batches",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("gateway_id", sa.String(length=120), nullable=False),
            sa.Column("requested_by", sa.String(length=320), nullable=False),
            sa.Column("approved_by", sa.String(length=320), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("write_count", sa.Integer(), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_bacnet_write_batches_gateway_id", "bacnet_write_batches", ["gateway_id"], unique=False)
        op.create_index("ix_bacnet_write_batches_status", "bacnet_write_batches", ["status"], unique=False)

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("bacnet_write_commands"):
        op.create_table(
            "bacnet_write_commands",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("batch_id", sa.String(length=36), nullable=False),
            sa.Column("edge_job_id", sa.String(length=80), nullable=False),
            sa.Column("gateway_id", sa.String(length=120), nullable=False),
            sa.Column("saved_point_id", sa.String(length=36), nullable=False),
            sa.Column("device_instance", sa.Integer(), nullable=False),
            sa.Column("object_type", sa.String(length=80), nullable=False),
            sa.Column("object_instance", sa.Integer(), nullable=False),
            sa.Column("property_name", sa.String(length=80), nullable=False),
            sa.Column("action", sa.String(length=40), nullable=False),
            sa.Column("requested_value", sa.JSON(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.String(length=1000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["batch_id"], ["bacnet_write_batches.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_bacnet_write_commands_batch_id", "bacnet_write_commands", ["batch_id"], unique=False)
        op.create_index("ix_bacnet_write_commands_edge_job_id", "bacnet_write_commands", ["edge_job_id"], unique=False)
        op.create_index("ix_bacnet_write_commands_gateway_id", "bacnet_write_commands", ["gateway_id"], unique=False)
        op.create_index("ix_bacnet_write_commands_saved_point_id", "bacnet_write_commands", ["saved_point_id"], unique=False)
        op.create_index("ix_bacnet_write_commands_status", "bacnet_write_commands", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bacnet_write_commands_status", table_name="bacnet_write_commands")
    op.drop_index("ix_bacnet_write_commands_saved_point_id", table_name="bacnet_write_commands")
    op.drop_index("ix_bacnet_write_commands_gateway_id", table_name="bacnet_write_commands")
    op.drop_index("ix_bacnet_write_commands_edge_job_id", table_name="bacnet_write_commands")
    op.drop_index("ix_bacnet_write_commands_batch_id", table_name="bacnet_write_commands")
    op.drop_table("bacnet_write_commands")
    op.drop_index("ix_bacnet_write_batches_status", table_name="bacnet_write_batches")
    op.drop_index("ix_bacnet_write_batches_gateway_id", table_name="bacnet_write_batches")
    op.drop_table("bacnet_write_batches")
    for name in ("state_text", "relinquish_default", "priority_array", "active_priority"):
        op.drop_column("saved_bacnet_points", name)
