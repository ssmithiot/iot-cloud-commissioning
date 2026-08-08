"""Add immutable component commits to gateway update requests.

Revision ID: 0024_gateway_update_target_commits
Revises: 0023_edge_release_targets
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0024_gateway_update_target_commits"
down_revision: str | Sequence[str] | None = "0023_edge_release_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("gateway_update_requests")}
    if "target_ui_commit" not in columns:
        op.add_column("gateway_update_requests", sa.Column("target_ui_commit", sa.String(length=40), nullable=True))
    if "target_agent_commit" not in columns:
        op.add_column("gateway_update_requests", sa.Column("target_agent_commit", sa.String(length=40), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("gateway_update_requests")}
    if "target_agent_commit" in columns:
        op.drop_column("gateway_update_requests", "target_agent_commit")
    if "target_ui_commit" in columns:
        op.drop_column("gateway_update_requests", "target_ui_commit")
