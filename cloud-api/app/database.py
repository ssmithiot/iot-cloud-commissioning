from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def engine_options_for(database_url: str) -> dict[str, object]:
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
    options.update(pool_size=2, max_overflow=0, pool_timeout=30, pool_recycle=300)
    return options

engine = create_engine(settings.database_url, **engine_options_for(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
