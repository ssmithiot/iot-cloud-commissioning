from collections.abc import Generator
from contextvars import ContextVar
import logging
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

pool_logger = logging.getLogger("iot-cloud-api.db_pool")
_pool_request_context: ContextVar[dict[str, str] | None] = ContextVar("pool_request_context", default=None)


def set_pool_request_context(request_id: str, method: str, route: str):
    return _pool_request_context.set({"request_id": request_id, "method": method, "route": route})


def reset_pool_request_context(token: object) -> None:
    _pool_request_context.reset(token)


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


def _pool_state() -> dict[str, int]:
    pool = engine.pool
    return {"checked_out": int(getattr(pool, "checkedout", lambda: 0)()), "idle": int(getattr(pool, "checkedin", lambda: 0)()), "overflow": int(getattr(pool, "overflow", lambda: 0)()), "pool_size": int(getattr(pool, "size", lambda: 0)()), "max_overflow": settings.db_max_overflow}


@event.listens_for(engine.pool, "checkout")
def _pool_checkout(dbapi_connection, connection_record, connection_proxy) -> None:  # type: ignore[no-untyped-def]
    context = _pool_request_context.get() or {"request_id": "background", "method": "BACKGROUND", "route": "background/unknown"}
    connection_record.info["iot_pool_owner"] = context
    connection_record.info["iot_pool_checked_out_at"] = time.monotonic()
    state = _pool_state()
    if state["checked_out"] >= state["pool_size"] + state["max_overflow"]:
        pool_logger.warning("DB_POOL_NEAR_EXHAUSTION request_id=%s method=%s route=%s checked_out=%s idle=%s overflow=%s pool_size=%s max_overflow=%s", context["request_id"], context["method"], context["route"], state["checked_out"], state["idle"], state["overflow"], state["pool_size"], state["max_overflow"])


@event.listens_for(engine.pool, "checkin")
def _pool_checkin(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    started = connection_record.info.pop("iot_pool_checked_out_at", None)
    context = connection_record.info.pop("iot_pool_owner", {"request_id": "background", "method": "BACKGROUND", "route": "background/unknown"})
    if started is None:
        return
    hold_ms = (time.monotonic() - started) * 1000
    if hold_ms >= 500:
        level = pool_logger.warning if hold_ms >= 1000 else pool_logger.info
        state = _pool_state()
        level("DB_POOL_SLOW_HOLD request_id=%s method=%s route=%s hold_ms=%.1f checked_out=%s idle=%s overflow=%s pool_size=%s max_overflow=%s", context["request_id"], context["method"], context["route"], hold_ms, state["checked_out"], state["idle"], state["overflow"], state["pool_size"], state["max_overflow"])


@event.listens_for(engine, "handle_error")
def _pool_error(exception_context) -> None:  # type: ignore[no-untyped-def]
    if "QueuePool limit" in str(exception_context.original_exception):
        context = _pool_request_context.get() or {"request_id": "background", "method": "BACKGROUND", "route": "background/unknown"}
        state = _pool_state()
        pool_logger.warning("DB_POOL_CHECKOUT_FAILURE request_id=%s method=%s route=%s checked_out=%s idle=%s overflow=%s pool_size=%s max_overflow=%s", context["request_id"], context["method"], context["route"], state["checked_out"], state["idle"], state["overflow"], state["pool_size"], state["max_overflow"])


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
