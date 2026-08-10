"""Add server-authoritative operator idle activity.

Revision ID: 0026_user_idle_activity
Revises: 0025_gateway_tunnel_requests
"""
from alembic import op
import sqlalchemy as sa

revision = "0026_user_idle_activity"
down_revision = "0025_gateway_tunnel_requests"
branch_labels = None
depends_on = None

def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("operator_users")}
    if "last_user_activity_at" not in columns:
        op.add_column("operator_users", sa.Column("last_user_activity_at", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("operator_users")}
    if "last_user_activity_at" in columns:
        op.drop_column("operator_users", "last_user_activity_at")
