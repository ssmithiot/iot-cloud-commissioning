"""Add stable Edge Live Device profile identity.

Revision ID: 0028_edge_profile_identity
Revises: 0027_device_template_roles
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_edge_profile_identity"
down_revision = "0027_device_template_roles"
branch_labels = None
depends_on = None

OLD_UNIQUE = "uq_saved_devices_gateway_instance"
INSTANCE_INDEX = "ix_saved_devices_gateway_instance"
PROFILE_INDEX = "uq_saved_devices_gateway_edge_profile"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("saved_bacnet_devices")}
    if "edge_device_profile_id" not in columns:
        op.add_column("saved_bacnet_devices", sa.Column("edge_device_profile_id", sa.String(length=255), nullable=True))
    uniques = {item["name"] for item in inspector.get_unique_constraints("saved_bacnet_devices")}
    if OLD_UNIQUE in uniques:
        # SQLite cannot ALTER a constraint in place; batch mode rebuilds the
        # table while preserving every existing row.
        with op.batch_alter_table("saved_bacnet_devices") as batch:
            batch.drop_constraint(OLD_UNIQUE, type_="unique")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("saved_bacnet_devices")}
    if INSTANCE_INDEX not in indexes:
        op.create_index(INSTANCE_INDEX, "saved_bacnet_devices", ["gateway_id", "device_instance"], unique=False)
    if PROFILE_INDEX not in indexes:
        op.create_index(
            PROFILE_INDEX,
            "saved_bacnet_devices",
            ["gateway_id", "edge_device_profile_id"],
            unique=True,
            postgresql_where=sa.text("edge_device_profile_id IS NOT NULL"),
            sqlite_where=sa.text("edge_device_profile_id IS NOT NULL"),
        )


def downgrade():
    bind = op.get_bind()
    duplicate = bind.execute(sa.text("""
        SELECT gateway_id, device_instance
        FROM saved_bacnet_devices
        GROUP BY gateway_id, device_instance
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError("Cannot restore uq_saved_devices_gateway_instance while mirrored Edge profiles share a BACnet device instance")
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("saved_bacnet_devices")}
    if PROFILE_INDEX in indexes:
        op.drop_index(PROFILE_INDEX, table_name="saved_bacnet_devices")
    if INSTANCE_INDEX in indexes:
        op.drop_index(INSTANCE_INDEX, table_name="saved_bacnet_devices")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("saved_bacnet_devices")}
    with op.batch_alter_table("saved_bacnet_devices") as batch:
        batch.create_unique_constraint(OLD_UNIQUE, ["gateway_id", "device_instance"])
        if "edge_device_profile_id" in columns:
            batch.drop_column("edge_device_profile_id")
