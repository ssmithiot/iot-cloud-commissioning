"""Alembic schema revision verification for the cloud application database."""

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


ALEMBIC_VERSION_TABLE = "alembic_version"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SchemaRevisionStatus:
    expected_revisions: tuple[str, ...]
    current_revisions: tuple[str, ...]
    auto_create_tables: bool

    @property
    def is_current(self) -> bool:
        return set(self.expected_revisions) == set(self.current_revisions)

    def as_dict(self) -> dict[str, object]:
        status = "development_auto_create" if self.auto_create_tables else ("ok" if self.is_current else "out_of_date")
        return {
            "status": status,
            "expected_revisions": list(self.expected_revisions),
            "current_revisions": list(self.current_revisions),
            "auto_create_tables": self.auto_create_tables,
            "migration_authority": "alembic",
        }


def expected_revisions() -> tuple[str, ...]:
    """Return Alembic's current head revisions from this deployed source tree."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    return tuple(sorted(script.get_heads()))


def current_revisions(engine: Engine) -> tuple[str, ...]:
    """Return revisions recorded in the target database without modifying it."""
    if not inspect(engine).has_table(ALEMBIC_VERSION_TABLE):
        return ()
    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}")).scalars().all()
    return tuple(sorted(str(row) for row in rows))


def schema_revision_status(engine: Engine, *, auto_create_tables: bool) -> SchemaRevisionStatus:
    return SchemaRevisionStatus(
        expected_revisions=expected_revisions(),
        current_revisions=current_revisions(engine),
        auto_create_tables=auto_create_tables,
    )


def require_current_schema(engine: Engine) -> SchemaRevisionStatus:
    """Fail startup when a managed database has not reached the Alembic head."""
    status = schema_revision_status(engine, auto_create_tables=False)
    if status.is_current:
        return status
    expected = ", ".join(status.expected_revisions) or "(no Alembic heads)"
    current = ", ".join(status.current_revisions) or "(no alembic_version row)"
    raise RuntimeError(
        "Database schema revision is not current. "
        f"Expected Alembic revision(s): {expected}; database has: {current}. "
        "Run `alembic upgrade head` using the same CLOUD_DATABASE_URL before starting the API."
    )
