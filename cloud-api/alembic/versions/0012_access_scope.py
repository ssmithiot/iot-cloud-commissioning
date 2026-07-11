"""organization and site access memberships

Revision ID: 0012_access_scope
Revises: 0011_edge_resource_metrics
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_access_scope"
down_revision = "0011_edge_resource_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    organization_id_type = next(column["type"] for column in inspector.get_columns("organizations") if column["name"] == "id")
    site_id_type = next(column["type"] for column in inspector.get_columns("sites") if column["name"] == "id")
    operator_id_type = next(column["type"] for column in inspector.get_columns("operator_users") if column["name"] == "id")

    if not inspector.has_table("organization_memberships"):
        op.create_table(
            "organization_memberships",
            sa.Column("id", operator_id_type, nullable=False),
            sa.Column("organization_id", organization_id_type, nullable=False),
            sa.Column("operator_user_id", operator_id_type, nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["operator_user_id"], ["operator_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "operator_user_id", name="uq_org_memberships_org_operator"),
        )
    if not inspector.has_table("site_memberships"):
        op.create_table(
            "site_memberships",
            sa.Column("id", operator_id_type, nullable=False),
            sa.Column("site_uuid", site_id_type, nullable=False),
            sa.Column("operator_user_id", operator_id_type, nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["site_uuid"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["operator_user_id"], ["operator_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("site_uuid", "operator_user_id", name="uq_site_memberships_site_operator"),
        )

    inspector = sa.inspect(op.get_bind())
    organization_indexes = {index["name"] for index in inspector.get_indexes("organization_memberships")}
    if "ix_organization_memberships_organization_id" not in organization_indexes:
        op.create_index("ix_organization_memberships_organization_id", "organization_memberships", ["organization_id"], unique=False)
    if "ix_organization_memberships_operator_user_id" not in organization_indexes:
        op.create_index("ix_organization_memberships_operator_user_id", "organization_memberships", ["operator_user_id"], unique=False)
    site_indexes = {index["name"] for index in inspector.get_indexes("site_memberships")}
    if "ix_site_memberships_site_uuid" not in site_indexes:
        op.create_index("ix_site_memberships_site_uuid", "site_memberships", ["site_uuid"], unique=False)
    if "ix_site_memberships_operator_user_id" not in site_indexes:
        op.create_index("ix_site_memberships_operator_user_id", "site_memberships", ["operator_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_site_memberships_operator_user_id", table_name="site_memberships")
    op.drop_index("ix_site_memberships_site_uuid", table_name="site_memberships")
    op.drop_table("site_memberships")
    op.drop_index("ix_organization_memberships_operator_user_id", table_name="organization_memberships")
    op.drop_index("ix_organization_memberships_organization_id", table_name="organization_memberships")
    op.drop_table("organization_memberships")
