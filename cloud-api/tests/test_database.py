import os

os.environ.setdefault("GATEWAY_AUTH_PEPPER", "test-pepper")
os.environ.setdefault("IOT_ADMIN_API_TOKEN", "test-admin-token")

from app.database import connection_pool_status, engine_options_for


def test_non_sqlite_engine_limits_supabase_session_pool_usage() -> None:
    options = engine_options_for(
        "postgresql+psycopg://postgres:password@example.pooler.supabase.com:5432/postgres?sslmode=require",
        pool_size=3,
        max_overflow=1,
        pool_timeout_sec=45,
        pool_recycle_sec=600,
    )

    assert options["pool_size"] == 3
    assert options["max_overflow"] == 1
    assert options["pool_timeout"] == 45
    assert options["pool_recycle"] == 600
    assert options["pool_pre_ping"] is True


def test_sqlite_engine_keeps_sqlite_connection_options() -> None:
    options = engine_options_for("sqlite:///./test.db")

    assert options == {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}


def test_connection_pool_status_reports_non_sensitive_metrics() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    try:
        status = connection_pool_status(engine)
    finally:
        engine.dispose()

    assert status["implementation"]
