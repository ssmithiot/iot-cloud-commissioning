"""Coverage for migration 0019_uuid_schema_alignment.

Two guarantees:

1. The migration's TARGET_UUID_COLUMNS map stays in lockstep with the
   models: every CloudUUID-mapped column, and every foreign-key column
   referencing one, is listed — no more, no less. Adding a CloudUUID
   column without updating the migration map fails this test.

2. A database built purely from the migration chain contains every table
   the models declare. This is what staging proved wrong on 2026-07-14:
   ``gateway_credentials`` and ``site_weather`` existed in production only
   via ``create_all`` and were absent from migration-built databases.

Note: SQLite cannot validate the uuid-vs-varchar column *types* (its
CloudUUID impl is String(36)); the type conversions are PostgreSQL-only
and guarded by live inspection inside the migration itself.
"""

import importlib.util
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

os.environ.setdefault("DATABASE_URL", "sqlite:///./test-cloud-api.db")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")
os.environ.setdefault("GATEWAY_AUTH_PEPPER", "test-pepper")
os.environ.setdefault("IOT_ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-supabase-jwt-secret")

from app.models import Base, CloudUUID  # noqa: E402

CLOUD_API_DIR = Path(__file__).resolve().parents[1]
MIGRATION_PATH = CLOUD_API_DIR / "alembic" / "versions" / "0019_uuid_schema_alignment.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("migration_0019", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_uuid_columns() -> dict[str, set[str]]:
    """Every CloudUUID column plus FK columns referencing a CloudUUID column."""
    expected: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            is_uuid = isinstance(column.type, CloudUUID)
            references_uuid = any(
                isinstance(fk.column.type, CloudUUID) for fk in column.foreign_keys
            )
            if is_uuid or references_uuid:
                expected.setdefault(table.name, set()).add(column.name)
    return expected


def test_target_uuid_columns_match_models() -> None:
    migration = _load_migration_module()
    declared = {table: set(columns) for table, columns in migration.TARGET_UUID_COLUMNS.items()}
    expected = _model_uuid_columns()
    assert declared == expected, (
        "TARGET_UUID_COLUMNS in 0019_uuid_schema_alignment is out of sync with "
        "app.models CloudUUID usage.\n"
        f"missing from migration: { {t: sorted(c - declared.get(t, set())) for t, c in expected.items() if c - declared.get(t, set())} }\n"
        f"extra in migration: { {t: sorted(c - expected.get(t, set())) for t, c in declared.items() if c - expected.get(t, set())} }"
    )


def test_migration_revision_chain() -> None:
    migration = _load_migration_module()
    assert migration.revision == "0019_uuid_schema_alignment"
    assert migration.down_revision == "0018_backfill_received_at"


@pytest.fixture()
def migrated_sqlite_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path}"
    # alembic/env.py takes its URL from app.config.settings.database_url,
    # not from the Config object, so patch the live settings.
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    config = Config(str(CLOUD_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(CLOUD_API_DIR / "alembic"))
    command.upgrade(config, "head")
    return url


def test_migration_built_database_contains_every_model_table(migrated_sqlite_url: str) -> None:
    engine = sa.create_engine(migrated_sqlite_url)
    try:
        migrated_tables = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
    model_tables = set(Base.metadata.tables.keys())
    missing = sorted(model_tables - migrated_tables)
    assert not missing, (
        f"Tables declared by the models but absent from a migration-built database: {missing}. "
        "Every new model table needs a migration (create_all only papers over this in "
        "AUTO_CREATE_TABLES environments)."
    )


def test_migration_upgrade_is_idempotent_on_migrated_database(migrated_sqlite_url: str) -> None:
    """Re-running upgrade against an already-aligned database must be safe.

    Exercises the has_table guards (SQLite path). The PostgreSQL type
    conversions are separately guarded by live type inspection.
    """
    from app.config import settings

    assert settings.database_url == migrated_sqlite_url  # patched by the fixture
    config = Config(str(CLOUD_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(CLOUD_API_DIR / "alembic"))
    command.downgrade(config, "0018_backfill_received_at")
    command.upgrade(config, "head")
