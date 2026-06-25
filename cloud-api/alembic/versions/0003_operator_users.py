"""operator users

Revision ID: 0003_operator_users
Revises: 0002_edge_jobs
Create Date: 2026-06-25
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_operator_users"
down_revision = "0002_edge_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("supabase_user_id", sa.String(length=120), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_operator_users_email"),
    )
    op.create_index(op.f("ix_operator_users_email"), "operator_users", ["email"], unique=False)
    op.create_index(op.f("ix_operator_users_supabase_user_id"), "operator_users", ["supabase_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_operator_users_supabase_user_id"), table_name="operator_users")
    op.drop_index(op.f("ix_operator_users_email"), table_name="operator_users")
    op.drop_table("operator_users")
