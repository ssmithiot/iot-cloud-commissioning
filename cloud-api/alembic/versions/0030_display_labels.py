"""Add customer-facing labels to saved points and mapping rules.

Revision ID: 0030_display_labels
Revises: 0029_mapping_templates
"""
from alembic import op
import sqlalchemy as sa


revision = "0030_display_labels"
down_revision = "0029_mapping_templates"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("saved_bacnet_points") as batch:
        batch.add_column(sa.Column("display_label", sa.String(length=120), nullable=True))
    with op.batch_alter_table("mapping_template_rules") as batch:
        batch.add_column(sa.Column("display_label", sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table("mapping_template_rules") as batch:
        batch.drop_column("display_label")
    with op.batch_alter_table("saved_bacnet_points") as batch:
        batch.drop_column("display_label")
