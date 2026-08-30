"""Configuration-only backup and restore for the mixed Edge trend database."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import sqlite3


CONFIG_TABLES = ("trend_groups", "trend_points", "trend_views")
HISTORY_TABLES = ("trend_runs", "trend_samples", "trend_upload_outbox", "trend_sync_state")
SNAPSHOT_FILENAME = "updater-trend-config.sqlite"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_ident(table)})")]


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _table_rows(connection: sqlite3.Connection, table: str, columns: list[str]) -> list[tuple[object, ...]]:
    column_sql = ", ".join(_ident(column) for column in columns)
    return list(connection.execute(f"SELECT {column_sql} FROM {_ident(table)} ORDER BY id"))


def create_trend_config_snapshot(source_path: str | Path, destination_path: str | Path) -> dict[str, int]:
    """Copy only trend configuration using one consistent SQLite read transaction."""
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    temporary_path = destination_path.with_name(destination_path.name + ".creating")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (temporary_path, destination_path):
        path.unlink(missing_ok=True)

    source = _read_only(source_path)
    destination: sqlite3.Connection | None = None
    try:
        source.execute("BEGIN")
        table_sql: dict[str, str] = {}
        index_sql: list[str] = []
        columns: dict[str, list[str]] = {}
        rows: dict[str, list[tuple[object, ...]]] = {}
        for table in CONFIG_TABLES:
            schema = source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if schema is None or not schema[0]:
                raise RuntimeError(f"Live trend database is missing required table {table}")
            table_sql[table] = str(schema[0])
            columns[table] = _columns(source, table)
            rows[table] = _table_rows(source, table, columns[table])
        for table in CONFIG_TABLES:
            index_sql.extend(
                str(row[0])
                for row in source.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name",
                    (table,),
                )
            )
        user_version = int(source.execute("PRAGMA user_version").fetchone()[0])

        destination = sqlite3.connect(temporary_path)
        destination.execute("PRAGMA foreign_keys=OFF")
        for table in CONFIG_TABLES:
            destination.execute(table_sql[table])
            if rows[table]:
                column_sql = ", ".join(_ident(column) for column in columns[table])
                placeholders = ", ".join("?" for _ in columns[table])
                destination.executemany(
                    f"INSERT INTO {_ident(table)} ({column_sql}) VALUES ({placeholders})",
                    rows[table],
                )
        for statement in index_sql:
            destination.execute(statement)
        destination.execute(f"PRAGMA user_version={user_version}")
        destination.commit()
        destination.execute("PRAGMA foreign_keys=ON")
        violations = list(destination.execute("PRAGMA foreign_key_check"))
        if violations:
            raise RuntimeError(f"Trend configuration snapshot foreign-key validation failed: {violations!r}")
        actual_tables = {
            str(row[0])
            for row in destination.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_tables != set(CONFIG_TABLES):
            raise RuntimeError(f"Unexpected trend snapshot tables: {sorted(actual_tables)!r}")
        counts = {table: int(destination.execute(f"SELECT COUNT(*) FROM {_ident(table)}").fetchone()[0]) for table in CONFIG_TABLES}
        expected = {table: len(rows[table]) for table in CONFIG_TABLES}
        if counts != expected:
            raise RuntimeError(f"Trend configuration snapshot row-count mismatch: {counts!r} != {expected!r}")
        destination.close()
        destination = None
        os.replace(temporary_path, destination_path)
        return counts
    except Exception:
        temporary_path.unlink(missing_ok=True)
        destination_path.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        source.rollback()
        source.close()


def _delete_ids_not_in(connection: sqlite3.Connection, table: str, ids: set[int]) -> None:
    if not ids:
        connection.execute(f"DELETE FROM {_ident(table)}")
        return
    placeholders = ", ".join("?" for _ in ids)
    connection.execute(f"DELETE FROM {_ident(table)} WHERE id NOT IN ({placeholders})", tuple(sorted(ids)))


def _upsert_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: list[tuple[object, ...]],
) -> None:
    id_index = columns.index("id")
    mutable = [column for column in columns if column != "id"]
    assignments = ", ".join(f"{_ident(column)}=?" for column in mutable)
    insert_columns = ", ".join(_ident(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    for row in rows:
        row_id = row[id_index]
        values = [row[columns.index(column)] for column in mutable]
        cursor = connection.execute(
            f"UPDATE {_ident(table)} SET {assignments} WHERE id=?",
            (*values, row_id),
        )
        if cursor.rowcount == 0:
            connection.execute(
                f"INSERT INTO {_ident(table)} ({insert_columns}) VALUES ({placeholders})",
                row,
            )


def restore_trend_config(snapshot_path: str | Path, live_path: str | Path) -> dict[str, int]:
    """Restore only configuration tables while preserving all live history rows."""
    snapshot = _read_only(Path(snapshot_path))
    live = sqlite3.connect(Path(live_path), timeout=30)
    try:
        snapshot.execute("BEGIN")
        snapshot_columns: dict[str, list[str]] = {}
        snapshot_rows: dict[str, list[tuple[object, ...]]] = {}
        for table in CONFIG_TABLES:
            if snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is None:
                raise RuntimeError(f"Trend configuration backup is missing required table {table}")
            snapshot_columns[table] = _columns(snapshot, table)
            snapshot_rows[table] = _table_rows(snapshot, table, snapshot_columns[table])

        group_ids = {int(row[snapshot_columns["trend_groups"].index("id")]) for row in snapshot_rows["trend_groups"]}
        point_ids = {int(row[snapshot_columns["trend_points"].index("id")]) for row in snapshot_rows["trend_points"]}
        point_group_index = snapshot_columns["trend_points"].index("group_id")
        view_group_index = snapshot_columns["trend_views"].index("group_id")
        if any(int(row[point_group_index]) not in group_ids for row in snapshot_rows["trend_points"]):
            raise RuntimeError("Trend configuration backup contains a point with no group")
        if any(int(row[view_group_index]) not in group_ids for row in snapshot_rows["trend_views"]):
            raise RuntimeError("Trend configuration backup contains a view with no group")

        live.execute("PRAGMA foreign_keys=ON")
        live.execute("BEGIN IMMEDIATE")
        for table in (*CONFIG_TABLES, *HISTORY_TABLES):
            if live.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                raise RuntimeError(f"Live trend database is missing required table {table}")
        for table in CONFIG_TABLES:
            live_columns = _columns(live, table)
            if live_columns != snapshot_columns[table]:
                raise RuntimeError(
                    f"Trend configuration schema mismatch for {table}: {snapshot_columns[table]!r} != {live_columns!r}"
                )

        sample_point_ids = {int(row[0]) for row in live.execute("SELECT DISTINCT trend_point_id FROM trend_samples")}
        run_group_ids = {int(row[0]) for row in live.execute("SELECT DISTINCT group_id FROM trend_runs")}
        if not sample_point_ids.issubset(point_ids):
            raise RuntimeError("Backup omits trend point IDs referenced by current samples")
        if not run_group_ids.issubset(group_ids):
            raise RuntimeError("Backup omits trend group IDs referenced by current runs")

        view_ids = {int(row[snapshot_columns["trend_views"].index("id")]) for row in snapshot_rows["trend_views"]}
        _delete_ids_not_in(live, "trend_views", view_ids)
        _delete_ids_not_in(live, "trend_points", point_ids)
        _delete_ids_not_in(live, "trend_groups", group_ids)
        for table in CONFIG_TABLES:
            _upsert_rows(live, table, snapshot_columns[table], snapshot_rows[table])

        violations = list(live.execute("PRAGMA foreign_key_check"))
        if violations:
            raise RuntimeError(f"Live trend database foreign-key validation failed: {violations!r}")
        counts = {table: int(live.execute(f"SELECT COUNT(*) FROM {_ident(table)}").fetchone()[0]) for table in CONFIG_TABLES}
        expected = {table: len(snapshot_rows[table]) for table in CONFIG_TABLES}
        if counts != expected:
            raise RuntimeError(f"Restored trend configuration row-count mismatch: {counts!r} != {expected!r}")
        live.commit()
        return counts
    except Exception:
        live.rollback()
        raise
    finally:
        snapshot.rollback()
        snapshot.close()
        live.close()


def _embedded_source(*functions: object) -> str:
    prefix = (
        "from __future__ import annotations\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sqlite3\n\n"
        f"CONFIG_TABLES = {CONFIG_TABLES!r}\n"
        f"HISTORY_TABLES = {HISTORY_TABLES!r}\n\n"
    )
    return prefix + "\n\n".join(inspect.getsource(function) for function in functions)


def create_snapshot_script(source_path: str, destination_path: str) -> str:
    source = _embedded_source(_ident, _columns, _read_only, _table_rows, create_trend_config_snapshot)
    return source + f"\ncounts = create_trend_config_snapshot({source_path!r}, {destination_path!r})\n" + (
        "print('TREND_CONFIG_BACKUP=created')\n"
        "print('TREND_CONFIG_GROUPS=' + str(counts['trend_groups']))\n"
        "print('TREND_CONFIG_POINTS=' + str(counts['trend_points']))\n"
        "print('TREND_CONFIG_VIEWS=' + str(counts['trend_views']))\n"
    )


def restore_config_script(snapshot_path: str | None, live_path: str) -> str:
    source = _embedded_source(_ident, _columns, _read_only, _table_rows, _delete_ids_not_in, _upsert_rows, restore_trend_config)
    snapshot_expression = "os.environ['TREND_CONFIG_SOURCE']" if snapshot_path is None else repr(snapshot_path)
    return source + f"\ncounts = restore_trend_config({snapshot_expression}, {live_path!r})\n" + (
        "print('TREND_CONFIG_RESTORE=restored')\n"
        "print('TREND_CONFIG_GROUPS=' + str(counts['trend_groups']))\n"
        "print('TREND_CONFIG_POINTS=' + str(counts['trend_points']))\n"
        "print('TREND_CONFIG_VIEWS=' + str(counts['trend_views']))\n"
        "print('TREND_HISTORY_RESTORE=preserved_current')\n"
    )
