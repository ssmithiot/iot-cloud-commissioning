"""site direct connect metadata

Revision ID: 0006_site_direct_connect
Revises: 0005_site_metadata
Create Date: 2026-06-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_site_direct_connect"
down_revision = "0005_site_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("cradlepoint_ip", sa.String(length=255), nullable=True))
    op.add_column("sites", sa.Column("direct_connect_host", sa.String(length=255), nullable=True))
    op.add_column("sites", sa.Column("direct_connect_port", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("gateway_ui_port", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("store_hours_monday_friday", sa.String(length=120), nullable=True))
    op.add_column("sites", sa.Column("store_hours_saturday", sa.String(length=120), nullable=True))
    op.add_column("sites", sa.Column("store_hours_sunday", sa.String(length=120), nullable=True))
    op.add_column("sites", sa.Column("network_status_notes", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "network_status_notes")
    op.drop_column("sites", "store_hours_sunday")
    op.drop_column("sites", "store_hours_saturday")
    op.drop_column("sites", "store_hours_monday_friday")
    op.drop_column("sites", "gateway_ui_port")
    op.drop_column("sites", "direct_connect_port")
    op.drop_column("sites", "direct_connect_host")
    op.drop_column("sites", "cradlepoint_ip")
