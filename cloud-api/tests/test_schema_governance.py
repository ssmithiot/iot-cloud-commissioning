import pytest
from sqlalchemy import create_engine, text

from app.schema import expected_revisions, require_current_schema, schema_revision_status
from app.schemas import SavedPointsReadIn


def test_schema_status_reports_development_auto_create_without_alembic_table() -> None:
    engine = create_engine("sqlite://")

    status = schema_revision_status(engine, auto_create_tables=True)

    assert status.auto_create_tables is True
    assert status.current_revisions == ()
    assert status.as_dict()["status"] == "development_auto_create"
    assert status.as_dict()["migration_authority"] == "alembic"


def test_require_current_schema_accepts_alembic_head_and_rejects_drift() -> None:
    engine = create_engine("sqlite://")
    expected = expected_revisions()
    assert expected
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        for revision in expected:
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES (:revision)"), {"revision": revision})

    assert require_current_schema(engine).is_current is True

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('not-a-real-revision')"))

    with pytest.raises(RuntimeError, match="Database schema revision is not current"):
        require_current_schema(engine)


def test_saved_point_read_request_accepts_a_full_controller_point_list() -> None:
    request = SavedPointsReadIn(point_ids=["saved-point"] * 222)

    assert len(request.point_ids) == 222
