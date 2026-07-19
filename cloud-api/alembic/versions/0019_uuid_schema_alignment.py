"""uuid schema alignment — migration-built databases match the models

Revision ID: 0019_uuid_schema_alignment
Revises: 0018_backfill_received_at

Background (2026-07-14, staging bring-up): production tables were originally
created from the models via ``create_all`` (CloudUUID -> PostgreSQL UUID),
but the migration chain declares several of those primary/foreign keys as
``sa.Integer`` (0001, 0002) or ``sa.String(36)`` (0003, 0009, 0014, 0017).
SQLite accepts anything (its CloudUUID impl is String(36) and it does not
enforce column types), so the CI migration round-trip could not catch it.
The first database ever built purely from migrations on PostgreSQL —
staging — failed on ``/api/auth/register`` with a uuid/text mismatch on
``operator_users``.

Additionally, ``gateway_credentials`` and ``site_weather`` were never
created by any migration; they exist in production only via ``create_all``.

This migration:
1. Creates ``gateway_credentials`` and ``site_weather`` when absent
   (all dialects, model-accurate DDL).
2. On PostgreSQL only, converts every drifted CloudUUID-mapped column to
   ``uuid``, dropping and re-creating affected foreign keys. Columns already
   ``uuid`` (production) are skipped, so this is a no-op there. Integer
   columns are only converted when their table is empty (fresh
   migration-built databases); a populated Integer table aborts loudly
   rather than guessing.

Downgrade is intentionally a no-op: reversing would re-break the schema
against the models, and dropping ``gateway_credentials`` would destroy
credential data. The idempotent upgrade tolerates re-runs.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_uuid_schema_alignment"
down_revision = "0018_backfill_received_at"
branch_labels = None
depends_on = None


# Every column the models map with CloudUUID, plus the foreign-key columns
# that reference one. tests/test_uuid_schema_alignment.py asserts this map
# stays in sync with app.models.
TARGET_UUID_COLUMNS: dict[str, tuple[str, ...]] = {
    "organizations": ("id",),
    "sites": ("id", "organization_id"),
    "edge_nodes": ("id",),
    "edge_heartbeats": ("id", "edge_node_id"),
    "edge_jobs": ("id",),
    "operator_users": ("id",),
    "gateway_update_requests": ("id",),
    "gateway_alert_states": ("id",),
    "bacnet_write_batches": ("id",),
    "bacnet_write_commands": ("id", "batch_id"),
    "gateway_credentials": ("id",),
    "organization_memberships": ("id", "organization_id", "operator_user_id"),
    "site_memberships": ("id", "site_uuid", "operator_user_id"),
}


def _uuid_type(dialect_name: str):
    if dialect_name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(36)


def _scopes_type(dialect_name: str):
    if dialect_name == "postgresql":
        return postgresql.ARRAY(sa.String())
    return sa.JSON()


def _create_missing_tables(bind, inspector) -> None:
    dialect = bind.dialect.name

    if not inspector.has_table("gateway_credentials"):
        op.create_table(
            "gateway_credentials",
            sa.Column("id", _uuid_type(dialect), nullable=False),
            sa.Column("gateway_id", sa.String(length=120), nullable=False),
            sa.Column("token_prefix", sa.String(length=64), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=True),
            sa.Column("scopes", _scopes_type(dialect), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["gateway_id"], ["edge_nodes.gateway_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_prefix", name="uq_gateway_credentials_token_prefix"),
        )
        op.create_index(op.f("ix_gateway_credentials_gateway_id"), "gateway_credentials", ["gateway_id"], unique=False)
        op.create_index(op.f("ix_gateway_credentials_token_prefix"), "gateway_credentials", ["token_prefix"], unique=False)

    if not inspector.has_table("site_weather"):
        op.create_table(
            "site_weather",
            sa.Column("site_id", sa.String(length=120), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False),
            sa.Column("latitude", sa.Float(), nullable=False),
            sa.Column("longitude", sa.Float(), nullable=False),
            sa.Column("temperature_f", sa.Float(), nullable=True),
            sa.Column("apparent_temperature_f", sa.Float(), nullable=True),
            sa.Column("relative_humidity_percent", sa.Integer(), nullable=True),
            sa.Column("precipitation_in", sa.Float(), nullable=True),
            sa.Column("wind_speed_mph", sa.Float(), nullable=True),
            sa.Column("weather_code", sa.Integer(), nullable=True),
            sa.Column("condition", sa.String(length=120), nullable=True),
            sa.Column("timezone", sa.String(length=120), nullable=True),
            sa.Column("timezone_abbreviation", sa.String(length=40), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sunrise_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sunset_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("solar_noon_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["site_id"], ["sites.site_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("site_id"),
        )


def _postgres_type_conversions(bind, inspector) -> None:
    # Discover which target columns actually need conversion.
    pending: dict[tuple[str, str], str] = {}  # (table, column) -> kind: "string" | "integer"
    for table, columns in TARGET_UUID_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        current = {col["name"]: col["type"] for col in inspector.get_columns(table)}
        for column in columns:
            col_type = current.get(column)
            if col_type is None:
                continue
            type_name = str(col_type).lower()
            if "uuid" in type_name:
                continue  # already aligned (production)
            if "char" in type_name or "text" in type_name:
                pending[(table, column)] = "string"
            elif "int" in type_name:
                pending[(table, column)] = "integer"
            else:
                raise RuntimeError(
                    f"0019_uuid_schema_alignment: {table}.{column} has unexpected type "
                    f"{col_type!r}; refusing to convert automatically."
                )

    if not pending:
        return

    # Integer ids cannot be cast to uuid; they only exist on fresh
    # migration-built databases, which must be empty. Refuse otherwise.
    for (table, column), kind in sorted(pending.items()):
        if kind != "integer":
            continue
        row_count = bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
        if row_count:
            raise RuntimeError(
                f"0019_uuid_schema_alignment: {table}.{column} is Integer with "
                f"{row_count} row(s); cannot invent UUID values. Restore this "
                "database from a model-created source or clear the table first."
            )

    # Capture and drop every foreign key touching a converting column
    # (either side), so ALTER TYPE is permitted; re-create afterwards.
    saved_fks: list[tuple[str, dict]] = []
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            touches = any((table, col) in pending for col in fk["constrained_columns"]) or any(
                (fk["referred_table"], col) in pending for col in fk["referred_columns"]
            )
            if touches and fk.get("name"):
                saved_fks.append((table, fk))

    for table, fk in saved_fks:
        op.drop_constraint(fk["name"], table, type_="foreignkey")

    for (table, column), kind in sorted(pending.items()):
        # Serial defaults (nextval) from Integer PKs must go first; DROP
        # DEFAULT is a safe no-op when no default exists.
        op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT'))
        using = f'"{column}"::uuid' if kind == "string" else "NULL::uuid"
        op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE uuid USING {using}'))

    for table, fk in saved_fks:
        op.create_foreign_key(
            fk["name"],
            table,
            fk["referred_table"],
            fk["constrained_columns"],
            fk["referred_columns"],
            ondelete=(fk.get("options") or {}).get("ondelete"),
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _create_missing_tables(bind, inspector)
    if bind.dialect.name == "postgresql":
        # Re-inspect: tables may have been created above.
        _postgres_type_conversions(bind, sa.inspect(bind))


def downgrade() -> None:
    # Intentional no-op: see module docstring.
    pass
