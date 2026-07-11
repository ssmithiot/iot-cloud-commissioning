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
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sites")}
    for column in (
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
    ):
        if column.name not in existing:
            op.add_column("sites", column)


def downgrade() -> None:
    op.drop_column("sites", "longitude")
    op.drop_column("sites", "latitude")
