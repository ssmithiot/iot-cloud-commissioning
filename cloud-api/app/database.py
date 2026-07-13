from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def pool_engine_kwargs(database_url: str) -> dict[str, object]:
    """Return pool kwargs for server databases; SQLite keeps defaults.

    Pool controls apply to server databases only. SQLite (dev/tests) keeps
    SQLAlchemy defaults so in-memory and file databases behave as before.
    """
    if database_url.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_sec,
        "pool_recycle": settings.db_pool_recycle_sec,
    }


def connect_args_for(database_url: str) -> dict[str, object]:
    """Driver connect args per backend.

    - SQLite (dev/tests): allow cross-thread use under the test client.
    - PostgreSQL via psycopg3: disable automatic server-side prepared
      statements. They break behind transaction-mode poolers (Supabase
      Supavisor port 6543, PgBouncer), where consecutive statements may run
      on different server backends. Session-mode and direct connections work
      fine without them too; this makes the app safe on any pooler.
    """
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    if database_url.startswith("postgresql"):
        return {"prepare_threshold": None}
    return {}


engine = create_engine(
    settings.database_url,
    connect_args=connect_args_for(settings.database_url),
    pool_pre_ping=True,
    **pool_engine_kwargs(settings.database_url),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
