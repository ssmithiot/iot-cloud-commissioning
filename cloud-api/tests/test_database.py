import os

os.environ.setdefault("GATEWAY_AUTH_PEPPER", "test-pepper")
os.environ.setdefault("IOT_ADMIN_API_TOKEN", "test-admin-token")

from app.database import engine_options_for


def test_non_sqlite_engine_limits_supabase_session_pool_usage() -> None:
    options = engine_options_for("postgresql+psycopg://postgres:password@example.pooler.supabase.com:5432/postgres?sslmode=require")

    assert options["pool_size"] == 2
    assert options["max_overflow"] == 0
    assert options["pool_timeout"] == 30
    assert options["pool_recycle"] == 300
    assert options["pool_pre_ping"] is True


def test_sqlite_engine_keeps_sqlite_connection_options() -> None:
    options = engine_options_for("sqlite:///./test.db")

    assert options == {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}
