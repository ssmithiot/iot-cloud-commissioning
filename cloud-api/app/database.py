from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def engine_options_for(
    database_url: str,
    *,
    pool_size: int = 2,
    max_overflow: int = 0,
    pool_timeout_sec: int = 30,
    pool_recycle_sec: int = 300,
) -> dict[str, object]:
    is_sqlite = database_url.startswith("sqlite")
    options: dict[str, object] = {
        "connect_args": {"check_same_thread": False} if is_sqlite else {},
        "pool_pre_ping": True,
    }
    if is_sqlite:
        return options

    # Supabase's session pool is intentionally small on the current plan.  The
    # SQLAlchemy defaults permit one API process to consume all 15 session
    # slots (five persistent connections plus ten overflow connections), which
    # prevents Render's Alembic pre-deploy task from acquiring its one
    # connection.  Keep the long-lived API footprint bounded.
    options.update(
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_sec,
        pool_recycle=pool_recycle_sec,
    )
    return options

engine = create_engine(
    settings.database_url,
    **engine_options_for(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_sec=settings.database_pool_timeout_sec,
        pool_recycle_sec=settings.database_pool_recycle_sec,
    ),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def connection_pool_status(database_engine: Engine) -> dict[str, int | str]:
    """Expose bounded, non-sensitive pool pressure metrics for health checks."""
    pool = database_engine.pool
    status: dict[str, int | str] = {"implementation": type(pool).__name__}
    for key, method_name in (("size", "size"), ("checked_in", "checkedin"), ("checked_out", "checkedout"), ("overflow", "overflow")):
        method = getattr(pool, method_name, None)
        if callable(method):
            status[key] = int(method())
    return status


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
