"""Migration 0030 preserves Phase 2 authority through a round trip."""
from pathlib import Path

from alembic import command
from alembic.config import Config
import sqlalchemy as sa


CLOUD_API_DIR = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    config = Config(str(CLOUD_API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(CLOUD_API_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_0030_nullable_labels_preserve_data_and_round_trip(tmp_path: Path) -> None:
    from app.config import settings

    database_url = f"sqlite:///{tmp_path / 'display-labels.db'}"
    config = _config(database_url)
    previous_database_url = settings.database_url
    settings.database_url = database_url
    point_id = "10000000-0000-0000-0000-000000000001"
    device_id = "20000000-0000-0000-0000-000000000002"
    template_id = "30000000-0000-0000-0000-000000000003"
    rule_id = "40000000-0000-0000-0000-000000000004"
    try:
        command.upgrade(config, "0029_mapping_templates")
        engine = sa.create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO mapping_templates (id, name, graphic_template_key, created_at, updated_at) "
                "VALUES (:id, 'Existing RTU', 'rtu', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": template_id})
            connection.execute(sa.text(
                "INSERT INTO mapping_template_rules "
                "(id, mapping_template_id, logical_role, match_field, match_value, object_type, required, created_at, updated_at) "
                "VALUES (:id, :template_id, 'space_temp', 'object_name', 'Room Temperature', 'analog-value', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": rule_id, "template_id": template_id})
            connection.execute(sa.text(
                "INSERT INTO saved_bacnet_devices "
                "(id, gateway_id, mapping_template_id, template_key, device_instance, lifecycle_state, enabled, created_at, updated_at) "
                "VALUES (:id, 'GW-MIGRATION', :template_id, 'rtu', 8650, 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": device_id, "template_id": template_id})
            connection.execute(sa.text(
                "INSERT INTO saved_bacnet_points "
                "(id, gateway_id, saved_device_id, device_instance, object_type, object_instance, logical_role, property_name, lifecycle_state, enabled, created_at, updated_at) "
                "VALUES (:id, 'GW-MIGRATION', :device_id, 8650, 'analog-value', 100, 'space_temp', 'present-value', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": point_id, "device_id": device_id})
        engine.dispose()

        command.upgrade(config, "0030_display_labels")
        engine = sa.create_engine(database_url)
        inspector = sa.inspect(engine)
        point_column = next(column for column in inspector.get_columns("saved_bacnet_points") if column["name"] == "display_label")
        rule_column = next(column for column in inspector.get_columns("mapping_template_rules") if column["name"] == "display_label")
        assert point_column["nullable"] is True and point_column["type"].length == 120
        assert rule_column["nullable"] is True and rule_column["type"].length == 120
        with engine.begin() as connection:
            point = connection.execute(sa.text(
                "SELECT id, saved_device_id, logical_role, display_label FROM saved_bacnet_points WHERE id=:id"
            ), {"id": point_id}).mappings().one()
            rule = connection.execute(sa.text(
                "SELECT id, mapping_template_id, logical_role, display_label FROM mapping_template_rules WHERE id=:id"
            ), {"id": rule_id}).mappings().one()
            association = connection.execute(sa.text(
                "SELECT mapping_template_id FROM saved_bacnet_devices WHERE id=:id"
            ), {"id": device_id}).scalar_one()
            assert dict(point) == {"id": point_id, "saved_device_id": device_id, "logical_role": "space_temp", "display_label": None}
            assert dict(rule) == {"id": rule_id, "mapping_template_id": template_id, "logical_role": "space_temp", "display_label": None}
            assert association == template_id
            connection.execute(sa.text("UPDATE saved_bacnet_points SET display_label='Sales Floor Temperature' WHERE id=:id"), {"id": point_id})
            connection.execute(sa.text("UPDATE mapping_template_rules SET display_label='Space Temperature' WHERE id=:id"), {"id": rule_id})
        engine.dispose()

        command.downgrade(config, "0029_mapping_templates")
        engine = sa.create_engine(database_url)
        inspector = sa.inspect(engine)
        assert "display_label" not in {column["name"] for column in inspector.get_columns("saved_bacnet_points")}
        assert "display_label" not in {column["name"] for column in inspector.get_columns("mapping_template_rules")}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT id, logical_role FROM saved_bacnet_points WHERE id=:id"), {"id": point_id}).one() == (point_id, "space_temp")
            assert connection.execute(sa.text("SELECT id, logical_role FROM mapping_template_rules WHERE id=:id"), {"id": rule_id}).one() == (rule_id, "space_temp")
            assert connection.execute(sa.text("SELECT mapping_template_id FROM saved_bacnet_devices WHERE id=:id"), {"id": device_id}).scalar_one() == template_id
        engine.dispose()

        command.upgrade(config, "0030_display_labels")
        engine = sa.create_engine(database_url)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT display_label FROM saved_bacnet_points WHERE id=:id"), {"id": point_id}).scalar_one_or_none() is None
            assert connection.execute(sa.text("SELECT display_label FROM mapping_template_rules WHERE id=:id"), {"id": rule_id}).scalar_one_or_none() is None
        engine.dispose()
    finally:
        settings.database_url = previous_database_url
