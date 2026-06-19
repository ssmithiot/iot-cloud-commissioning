"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id"),
    )
    op.create_index(op.f("ix_sites_site_id"), "sites", ["site_id"], unique=False)
    op.create_table(
        "edge_nodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("site_id", sa.String(length=120), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("lan_ip", sa.String(length=64), nullable=True),
        sa.Column("bacnet_port", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.String(length=80), nullable=False),
        sa.Column("ui_version", sa.String(length=80), nullable=False),
        sa.Column("sqlite_db_ok", sa.Boolean(), nullable=False),
        sa.Column("queued_upload_count", sa.Integer(), nullable=False),
        sa.Column("latest_status", sa.String(length=40), nullable=False),
        sa.Column("latest_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gateway_id", name="uq_edge_nodes_gateway_id"),
    )
    op.create_index(op.f("ix_edge_nodes_gateway_id"), "edge_nodes", ["gateway_id"], unique=False)
    op.create_index(op.f("ix_edge_nodes_site_id"), "edge_nodes", ["site_id"], unique=False)
    op.create_table(
        "edge_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("edge_node_id", sa.Integer(), nullable=False),
        sa.Column("gateway_id", sa.String(length=120), nullable=False),
        sa.Column("site_id", sa.String(length=120), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("lan_ip", sa.String(length=64), nullable=True),
        sa.Column("bacnet_port", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.String(length=80), nullable=False),
        sa.Column("ui_version", sa.String(length=80), nullable=False),
        sa.Column("sqlite_db_ok", sa.Boolean(), nullable=False),
        sa.Column("queued_upload_count", sa.Integer(), nullable=False),
        sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["edge_node_id"], ["edge_nodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_edge_heartbeats_edge_node_id"), "edge_heartbeats", ["edge_node_id"], unique=False)
    op.create_index(op.f("ix_edge_heartbeats_gateway_id"), "edge_heartbeats", ["gateway_id"], unique=False)
    op.create_index(op.f("ix_edge_heartbeats_site_id"), "edge_heartbeats", ["site_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_edge_heartbeats_site_id"), table_name="edge_heartbeats")
    op.drop_index(op.f("ix_edge_heartbeats_gateway_id"), table_name="edge_heartbeats")
    op.drop_index(op.f("ix_edge_heartbeats_edge_node_id"), table_name="edge_heartbeats")
    op.drop_table("edge_heartbeats")
    op.drop_index(op.f("ix_edge_nodes_site_id"), table_name="edge_nodes")
    op.drop_index(op.f("ix_edge_nodes_gateway_id"), table_name="edge_nodes")
    op.drop_table("edge_nodes")
    op.drop_index(op.f("ix_sites_site_id"), table_name="sites")
    op.drop_table("sites")
    op.drop_table("organizations")

