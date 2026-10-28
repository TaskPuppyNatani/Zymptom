import sqlite3
from contextlib import closing

import pytest

from zymptom.persistence import connect_database
from zymptom.persistence.database import transaction
from zymptom.persistence.migrations import MIGRATIONS, Migration, apply_migrations, schema_version


def test_fresh_database_creates_schema_and_records_version(connection):
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == {
        "schema_migrations", "episodes", "episode_functional_demands", "episode_symptoms",
        "episode_adaptations", "episode_consequences", "episode_recovery_actions",
        "sync_changes", "sync_heads", "sync_conflicts", "sync_acknowledgements",
        "sync_enrollment",
    }
    assert schema_version(connection) == MIGRATIONS[-1].version
    history = connection.execute("SELECT version, name, applied_at FROM schema_migrations").fetchall()
    assert len(history) == len(MIGRATIONS)
    assert history[0]["version"] == 1
    assert history[0]["name"] == "create_functional_episodes"
    assert history[0]["applied_at"].endswith("+00:00")
    assert history[1]["version"] == 2 and history[1]["name"] == "sync_contract_v1"
    assert history[2]["version"] == 3 and history[2]["name"] == "production_sync_enrollment"


def test_reopen_skips_applied_migration_and_preserves_data(database_path, full_episode):
    from zymptom.persistence import EpisodeRepository

    with closing(connect_database(database_path)) as first:
        EpisodeRepository(first).create(full_episode)
        first.execute("CREATE TABLE custom_marker (value TEXT)")
        first.execute("INSERT INTO custom_marker VALUES ('retained')")
        history = [tuple(row) for row in first.execute("SELECT * FROM schema_migrations")]
        original_schema_version = first.execute("PRAGMA schema_version").fetchone()[0]

    with closing(connect_database(database_path)) as reopened:
        assert [tuple(row) for row in reopened.execute("SELECT * FROM schema_migrations")] == history
        assert reopened.execute("PRAGMA schema_version").fetchone()[0] == original_schema_version
        assert reopened.execute("SELECT value FROM custom_marker").fetchone()[0] == "retained"
        assert reopened.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert EpisodeRepository(reopened).get(full_episode.id) == full_episode


def test_migrations_apply_in_number_order_and_only_once(tmp_path):
    first = Migration(1, "first", ("CREATE TABLE ordering (value INTEGER)", "INSERT INTO ordering VALUES (1)"))
    second = Migration(2, "second", ("INSERT INTO ordering VALUES (2)",))
    third = Migration(3, "third", ("INSERT INTO ordering VALUES (3)",))
    with closing(sqlite3.connect(tmp_path / "ordered.sqlite3", isolation_level=None)) as connection:
        assert schema_version(connection) == 0
        assert apply_migrations(connection, (second, first)) == 2
        assert apply_migrations(connection, (third, second, first)) == 3
        assert apply_migrations(connection, (first, second, third)) == 3
        assert connection.execute("SELECT value FROM ordering ORDER BY rowid").fetchall() == [(1,), (2,), (3,)]
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,)]
        assert schema_version(connection) == 3


def test_failed_fresh_migrations_roll_back_schema_and_history(tmp_path):
    first = Migration(1, "first", ("CREATE TABLE partial_schema (value INTEGER)",))
    failing = Migration(2, "broken", ("INSERT INTO partial_schema VALUES (1)", "INVALID SQL"))
    with closing(sqlite3.connect(tmp_path / "failed.sqlite3", isolation_level=None)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, (first, failing))
        assert not connection.in_transaction
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []
        assert schema_version(connection) == 0
        assert apply_migrations(connection, (first,)) == 1


def test_failed_upgrade_preserves_existing_schema_history_and_data(connection, repository, full_episode):
    repository.create(full_episode)
    history = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")]
    failing = Migration(MIGRATIONS[-1].version + 1, "broken_upgrade", (
        "CREATE TABLE partial_upgrade (value TEXT)",
        "UPDATE episodes SET activity = 'Should roll back'",
        "INVALID SQL",
    ))
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(connection, (*MIGRATIONS, failing))
    assert schema_version(connection) == MIGRATIONS[-1].version
    assert [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")] == history
    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'partial_upgrade'").fetchone() is None
    assert repository.get(full_episode.id) == full_episode


@pytest.mark.parametrize("versions", [(1, 1), (0,), (-1,), (True,)])
def test_invalid_version_registry_rejected(connection, versions):
    invalid = tuple(Migration(version, "invalid", ()) for version in versions)
    with pytest.raises(ValueError):
        apply_migrations(connection, invalid)
    assert schema_version(connection) == MIGRATIONS[-1].version


def test_unknown_database_version_rejected(connection):
    connection.execute("INSERT INTO schema_migrations VALUES (99, 'future_version', '2100-01-01T00:00:00+00:00')")
    with pytest.raises(RuntimeError, match="incompatible"):
        apply_migrations(connection)
    assert schema_version(connection) == 99


def test_migration_history_name_mismatch_rejected(connection):
    connection.execute("UPDATE schema_migrations SET name = 'different_history' WHERE version = 1")
    with pytest.raises(RuntimeError, match="incompatible"):
        apply_migrations(connection)


def test_transaction_rolls_back_even_on_interruption(connection):
    with pytest.raises(KeyboardInterrupt):
        with transaction(connection):
            connection.execute("CREATE TABLE interrupted (value TEXT)")
            raise KeyboardInterrupt
    assert not connection.in_transaction
    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'interrupted'").fetchone() is None


def test_in_memory_database_is_supported():
    with closing(connect_database(":memory:")) as connection:
        assert schema_version(connection) == MIGRATIONS[-1].version
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
