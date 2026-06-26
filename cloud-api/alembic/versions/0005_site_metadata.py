"""site metadata

Revision ID: 0005_site_metadata
Revises: 0004_gateway_tree
Create Date: 2026-06-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_site_metadata"
down_revision = "0004_gateway_tree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("external_ip", sa.String(length=64), nullable=True))
    op.add_column("sites", sa.Column("address", sa.String(length=500), nullable=True))
    op.add_column("sites", sa.Column("store_hours_mf", sa.String(length=120), nullable=True))
    op.add_column("sites", sa.Column("store_hours_sat", sa.String(length=120), nullable=True))
    op.add_column("sites", sa.Column("store_hours_sun", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "store_hours_sun")
    op.drop_column("sites", "store_hours_sat")
    op.drop_column("sites", "store_hours_mf")
    op.drop_column("sites", "address")
    op.drop_column("sites", "external_ip")
