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
    op.create_table(
        "organization_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("operator_user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operator_user_id"], ["operator_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "operator_user_id", name="uq_org_memberships_org_operator"),
    )
    op.create_index("ix_organization_memberships_organization_id", "organization_memberships", ["organization_id"], unique=False)
    op.create_index("ix_organization_memberships_operator_user_id", "organization_memberships", ["operator_user_id"], unique=False)
    op.create_table(
        "site_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("site_uuid", sa.String(length=36), nullable=False),
        sa.Column("operator_user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["site_uuid"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operator_user_id"], ["operator_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_uuid", "operator_user_id", name="uq_site_memberships_site_operator"),
    )
    op.create_index("ix_site_memberships_site_uuid", "site_memberships", ["site_uuid"], unique=False)
    op.create_index("ix_site_memberships_operator_user_id", "site_memberships", ["operator_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_site_memberships_operator_user_id", table_name="site_memberships")
    op.drop_index("ix_site_memberships_site_uuid", table_name="site_memberships")
    op.drop_table("site_memberships")
    op.drop_index("ix_organization_memberships_operator_user_id", table_name="organization_memberships")
    op.drop_index("ix_organization_memberships_organization_id", table_name="organization_memberships")
    op.drop_table("organization_memberships")
