"""site coordinate fields

Revision ID: 0008_site_coordinates
Revises: 0007_site_split_address
Create Date: 2026-07-09
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_site_coordinates"
down_revision = "0007_site_split_address"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("sites", sa.Column("longitude", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "longitude")
    op.drop_column("sites", "latitude")
