import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


os.environ.setdefault("GATEWAY_AUTH_PEPPER", "test-pepper")
os.environ.setdefault("IOT_ADMIN_API_TOKEN", "test-admin-token")

from app.config import settings
from app.database import Base
from app.schema import expected_revisions


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_migrations_reconcile_a_database_previously_changed_by_startup_ddl(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    config = _alembic_config(database_url)
    previous_database_url = settings.database_url
    settings.database_url = database_url
    try:
        command.upgrade(config, "0004_gateway_tree")

        # Simulate the formerly deployed startup path: it created new tables and
        # added columns without advancing alembic_version beyond 0004.
        engine = create_engine(database_url)
        Base.metadata.create_all(bind=engine)
        with sqlite3.connect(tmp_path / "legacy.db") as connection:
            # These tables did not exist before the Phase 1 migration.
            connection.execute("DROP TABLE site_memberships")
            connection.execute("DROP TABLE organization_memberships")
            for column in (
                "external_ip VARCHAR(64)", "address VARCHAR(500)",
                "store_hours_mf VARCHAR(120)", "store_hours_sat VARCHAR(120)", "store_hours_sun VARCHAR(120)",
                "cradlepoint_ip VARCHAR(255)", "direct_connect_host VARCHAR(255)",
                "direct_connect_port INTEGER", "gateway_ui_port INTEGER",
                "store_hours_monday_friday VARCHAR(120)", "store_hours_saturday VARCHAR(120)",
                "store_hours_sunday VARCHAR(120)", "network_status_notes VARCHAR(500)",
                "address_street VARCHAR(255)", "address_city VARCHAR(120)",
                "address_state VARCHAR(80)", "address_postal_code VARCHAR(40)", "latitude FLOAT", "longitude FLOAT",
            ):
                connection.execute(f"ALTER TABLE sites ADD COLUMN {column}")
            for table_name in ("edge_nodes", "edge_heartbeats"):
                for column in (
                    "cpu_count INTEGER", "cpu_load_1m FLOAT", "cpu_load_pct FLOAT",
                    "memory_used_pct FLOAT", "memory_available_mb INTEGER", "disk_used_pct FLOAT", "disk_free_mb INTEGER",
                ):
                    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column}")

        command.upgrade(config, "head")

        with sqlite3.connect(tmp_path / "legacy.db") as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (expected_revisions()[0],)
    finally:
        settings.database_url = previous_database_url
