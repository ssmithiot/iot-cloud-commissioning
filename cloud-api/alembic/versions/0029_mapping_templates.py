"""Add reusable point-mapping templates.

Revision ID: 0029_mapping_templates
Revises: 0028_edge_profile_identity
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_mapping_templates"
down_revision = "0028_edge_profile_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mapping_templates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("graphic_template_key", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mapping_template_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("mapping_template_id", sa.String(length=36), sa.ForeignKey("mapping_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logical_role", sa.String(length=80), nullable=False),
        sa.Column("match_field", sa.String(length=80), nullable=False),
        sa.Column("match_value", sa.String(length=255), nullable=False),
        sa.Column("object_type", sa.String(length=80), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mapping_template_id", "logical_role", name="uq_mapping_template_rule_role"),
    )
    op.create_index("ix_mapping_template_rules_template", "mapping_template_rules", ["mapping_template_id"])
    with op.batch_alter_table("saved_bacnet_devices") as batch:
        batch.add_column(sa.Column("mapping_template_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key("fk_saved_devices_mapping_template", "mapping_templates", ["mapping_template_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_saved_bacnet_devices_mapping_template_id", ["mapping_template_id"])


def downgrade():
    with op.batch_alter_table("saved_bacnet_devices") as batch:
        batch.drop_index("ix_saved_bacnet_devices_mapping_template_id")
        batch.drop_constraint("fk_saved_devices_mapping_template", type_="foreignkey")
        batch.drop_column("mapping_template_id")
    op.drop_index("ix_mapping_template_rules_template", table_name="mapping_template_rules")
    op.drop_table("mapping_template_rules")
    op.drop_table("mapping_templates")
