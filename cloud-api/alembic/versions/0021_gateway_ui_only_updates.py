"""Add UI-only gateway update scope.

Revision ID: 0021_gateway_ui_only_updates
Revises: 0020_gateway_identity_fp
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0021_gateway_ui_only_updates"
down_revision: str | Sequence[str] | None = "0020_gateway_identity_fp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("gateway_update_requests")}
    if "update_scope" not in columns:
        op.add_column("gateway_update_requests", sa.Column("update_scope", sa.String(length=20), nullable=False, server_default="agent"))
        # PostgreSQL can remove the temporary default after existing rows are
        # backfilled. SQLite cannot ALTER COLUMN; retaining this harmless
        # server default in SQLite keeps the migration chain testable.
        if op.get_bind().dialect.name != "sqlite":
            op.alter_column("gateway_update_requests", "update_scope", server_default=None)
    if "target_ui_version" not in columns:
        op.add_column("gateway_update_requests", sa.Column("target_ui_version", sa.String(length=80), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("gateway_update_requests")}
    if "target_ui_version" in columns:
        op.drop_column("gateway_update_requests", "target_ui_version")
    if "update_scope" in columns:
        op.drop_column("gateway_update_requests", "update_scope")
