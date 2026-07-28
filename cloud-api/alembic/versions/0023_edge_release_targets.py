"""Record the agent release targeted by a gateway update request.

Revision ID: 0023_edge_release_targets
Revises: 0022_edge_local_trend_samples
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0023_edge_release_targets"
down_revision: str | Sequence[str] | None = "0022_edge_local_trend_samples"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("gateway_update_requests")}
    if "target_agent_version" not in columns:
        op.add_column("gateway_update_requests", sa.Column("target_agent_version", sa.String(length=80), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("gateway_update_requests")}
    if "target_agent_version" in columns:
        op.drop_column("gateway_update_requests", "target_agent_version")
