"""Add full non-provisioning gateway update target.

Revision ID: 0022_gateway_full_non_provisioning_updates
Revises: 0021_gateway_ui_only_updates
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_gateway_full_non_provisioning_updates"
down_revision: str | Sequence[str] | None = "0021_gateway_ui_only_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("gateway_update_requests")}
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "gateway_update_requests",
            "update_scope",
            existing_type=sa.String(length=20),
            type_=sa.String(length=40),
            existing_nullable=False,
        )
    if "target_agent_version" not in columns:
        op.add_column("gateway_update_requests", sa.Column("target_agent_version", sa.String(length=80), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("gateway_update_requests")}
    if "target_agent_version" in columns:
        op.drop_column("gateway_update_requests", "target_agent_version")
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "gateway_update_requests",
            "update_scope",
            existing_type=sa.String(length=40),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
