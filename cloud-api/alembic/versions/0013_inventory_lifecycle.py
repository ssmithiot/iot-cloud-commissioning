"""inventory lifecycle metadata

Revision ID: 0013_inventory_lifecycle
Revises: 0012_access_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_inventory_lifecycle"
down_revision = "0012_access_scope"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    for table_name in ("saved_bacnet_devices", "saved_bacnet_points"):
        columns = _columns(table_name)
        if "first_seen_at" not in columns:
            op.add_column(table_name, sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
        if "last_seen_at" not in columns:
            op.add_column(table_name, sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        if "lifecycle_state" not in columns:
            op.add_column(
                table_name,
                sa.Column("lifecycle_state", sa.String(length=24), nullable=False, server_default="active"),
            )
        if "retired_at" not in columns:
            op.add_column(table_name, sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        index_name = f"ix_{table_name}_last_seen_at"
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}
        if index_name not in indexes:
            op.create_index(index_name, table_name, ["last_seen_at"], unique=False)
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET first_seen_at = COALESCE(first_seen_at, created_at), "
                f"last_seen_at = COALESCE(last_seen_at, updated_at, created_at), "
                "lifecycle_state = COALESCE(lifecycle_state, 'active')"
            )
        )


def downgrade() -> None:
    for table_name in ("saved_bacnet_points", "saved_bacnet_devices"):
        op.drop_index(f"ix_{table_name}_last_seen_at", table_name=table_name)
        op.drop_column(table_name, "retired_at")
        op.drop_column(table_name, "lifecycle_state")
        op.drop_column(table_name, "last_seen_at")
        op.drop_column(table_name, "first_seen_at")
