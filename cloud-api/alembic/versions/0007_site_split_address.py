"""site split address fields

Revision ID: 0007_site_split_address
Revises: 0006_site_direct_connect
Create Date: 2026-06-26
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_site_split_address"
down_revision = "0006_site_direct_connect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sites")}
    for column in (
        sa.Column("address_street", sa.String(length=255), nullable=True),
        sa.Column("address_city", sa.String(length=120), nullable=True),
        sa.Column("address_state", sa.String(length=80), nullable=True),
        sa.Column("address_postal_code", sa.String(length=40), nullable=True),
    ):
        if column.name not in existing:
            op.add_column("sites", column)


def downgrade() -> None:
    op.drop_column("sites", "address_postal_code")
    op.drop_column("sites", "address_state")
    op.drop_column("sites", "address_city")
    op.drop_column("sites", "address_street")
