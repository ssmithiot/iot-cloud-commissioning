"""gateway tree

Revision ID: 0004_gateway_tree
Revises: 0003_operator_users
Create Date: 2026-06-25
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_gateway_tree"
down_revision = "0003_operator_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gateway_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gateway_id", "name", name="uq_gateway_groups_gateway_name"),
    )
    op.create_index(op.f("ix_gateway_groups_gateway_id"), "gateway_groups", ["gateway_id"], unique=False)

    op.create_table(
        "saved_bacnet_devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=True),
        sa.Column("device_instance", sa.Integer(), nullable=False),
        sa.Column("device_name", sa.String(length=255), nullable=True),
        sa.Column("vendor_name", sa.String(length=255), nullable=True),
        sa.Column("network_number", sa.Integer(), nullable=True),
        sa.Column("mac_address", sa.String(length=255), nullable=True),
        sa.Column("latest_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["gateway_groups.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gateway_id", "device_instance", name="uq_saved_devices_gateway_instance"),
    )
    op.create_index(op.f("ix_saved_bacnet_devices_gateway_id"), "saved_bacnet_devices", ["gateway_id"], unique=False)

    op.create_table(
        "saved_bacnet_points",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("saved_device_id", sa.String(length=36), nullable=False),
        sa.Column("device_instance", sa.Integer(), nullable=False),
        sa.Column("object_type", sa.String(length=80), nullable=False),
        sa.Column("object_instance", sa.Integer(), nullable=False),
        sa.Column("object_name", sa.String(length=255), nullable=True),
        sa.Column("property_name", sa.String(length=80), nullable=False),
        sa.Column("present_value", sa.String(length=255), nullable=True),
        sa.Column("units", sa.String(length=80), nullable=True),
        sa.Column("writable", sa.Boolean(), nullable=True),
        sa.Column("latest_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["saved_device_id"], ["saved_bacnet_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "saved_device_id",
            "object_type",
            "object_instance",
            "property_name",
            name="uq_saved_points_device_object_property",
        ),
    )
    op.create_index(op.f("ix_saved_bacnet_points_gateway_id"), "saved_bacnet_points", ["gateway_id"], unique=False)
    op.create_index(op.f("ix_saved_bacnet_points_saved_device_id"), "saved_bacnet_points", ["saved_device_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_saved_bacnet_points_saved_device_id"), table_name="saved_bacnet_points")
    op.drop_index(op.f("ix_saved_bacnet_points_gateway_id"), table_name="saved_bacnet_points")
    op.drop_table("saved_bacnet_points")
    op.drop_index(op.f("ix_saved_bacnet_devices_gateway_id"), table_name="saved_bacnet_devices")
    op.drop_table("saved_bacnet_devices")
    op.drop_index(op.f("ix_gateway_groups_gateway_id"), table_name="gateway_groups")
    op.drop_table("gateway_groups")
