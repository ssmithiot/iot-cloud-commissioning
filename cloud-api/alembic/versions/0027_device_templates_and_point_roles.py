"""Add Cloud Phase 2 template and logical-role authority.

Revision ID: 0027_device_templates_and_point_roles
Revises: 0026_user_idle_activity
"""
from alembic import op
import sqlalchemy as sa

# ``alembic_version.version_num`` is VARCHAR(32) in the deployed schema.
revision = "0027_device_template_roles"
down_revision = "0026_user_idle_activity"
branch_labels = None
depends_on = None

INDEX = "uq_saved_points_device_logical_role"

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    device_columns = {column["name"] for column in inspector.get_columns("saved_bacnet_devices")}
    point_columns = {column["name"] for column in inspector.get_columns("saved_bacnet_points")}
    if "template_key" not in device_columns:
        op.add_column("saved_bacnet_devices", sa.Column("template_key", sa.String(length=80), nullable=True))
    if "logical_role" not in point_columns:
        op.add_column("saved_bacnet_points", sa.Column("logical_role", sa.String(length=80), nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("saved_bacnet_points")}
    if INDEX not in indexes:
        op.create_index(INDEX, "saved_bacnet_points", ["saved_device_id", "logical_role"], unique=True, postgresql_where=sa.text("logical_role IS NOT NULL"), sqlite_where=sa.text("logical_role IS NOT NULL"))

def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("saved_bacnet_points")}
    if INDEX in indexes:
        op.drop_index(INDEX, table_name="saved_bacnet_points")
    point_columns = {column["name"] for column in inspector.get_columns("saved_bacnet_points")}
    device_columns = {column["name"] for column in inspector.get_columns("saved_bacnet_devices")}
    if "logical_role" in point_columns:
        op.drop_column("saved_bacnet_points", "logical_role")
    if "template_key" in device_columns:
        op.drop_column("saved_bacnet_devices", "template_key")
