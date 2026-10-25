import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence import backups, migrations
from zymptom.persistence.backups import BackupError, BackupStore, copy_sqlite, readonly, validate_snapshot

NOW = datetime(2026, 10, 25, 23, 50, tzinfo=ZoneInfo("America/Los_Angeles"))


def contents(path):
    with closing(readonly(path)) as connection:
        connection.row_factory = sqlite3.Row
        return EpisodeRepository(connection).list()


def test_safe_wal_snapshot_contains_committed_episode_and_children(connection, repository, database_path, full_episode):
    connection.execute("PRAGMA journal_mode = WAL")
    full_episode.notes = "Unicode café ☕ 雪\nSecond line."
    repository.create(full_episode)
    store = BackupStore(database_path)
    with closing(sqlite3.connect(database_path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE episodes SET activity = 'Uncommitted'")
        info = store.create(connection, now=NOW)
        writer.rollback()
    assert info.valid and info.schema_version == 1
    assert info.size > 0 and len(info.checksum) == 64
    assert contents(info.path) == [full_episode]
    assert not info.path.with_name(info.path.name + '-wal').exists()
    assert not info.path.with_name(info.path.name + '-shm').exists()
    assert info.path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert validate_snapshot(info.path)[0] == 1
    metadata = json.loads(info.path.with_suffix(".json").read_text())
    assert metadata["validation"] == "ok" and metadata["size"] == info.size
    assert metadata["filename"] == info.path.name
    assert full_episode.activity not in json.dumps(metadata)


def test_daily_only_once_per_local_day_and_only_after_changes(connection, repository, database_path, full_episode):
    store = BackupStore(database_path)
    first = store.daily_if_changed(connection, now=NOW)
    assert first.kind == "daily"
    repository.create(full_episode)
    assert store.daily_if_changed(connection, now=NOW + timedelta(minutes=1)) is None
    second = store.daily_if_changed(connection, now=NOW + timedelta(minutes=20))
    assert second.path != first.path
    assert contents(second.path) == [full_episode]
    assert store.daily_if_changed(connection, now=NOW + timedelta(days=2)) is None
    with closing(connect_database(database_path)) as reopened:
        assert BackupStore(database_path).daily_if_changed(reopened, now=NOW + timedelta(days=3)) is None
    full_episode.notes = "Changed after the last daily backup"
    repository.update(full_episode)
    third = store.daily_if_changed(connection, now=NOW + timedelta(days=3))
    assert contents(third.path) == [full_episode]
    assert len(store.list()) == 3


def test_retention_keeps_manual_and_safety_and_newest_automatic(connection, database_path):
    store = BackupStore(database_path, retain=2)
    manual = store.create(connection, "manual", now=NOW)
    safety = store.create(connection, "pre-restore", now=NOW)
    automatic = [store.create(connection, "daily", now=NOW + timedelta(days=index)) for index in range(4)]
    assert {item.path for item in store.list()} == {manual.path, safety.path, automatic[-1].path, automatic[-2].path}
    assert BackupStore(database_path).retain == 30


def test_retention_keeps_only_good_backup_even_with_zero_target(connection, database_path):
    store = BackupStore(database_path, retain=0)
    good = store.create(connection, "daily")
    store.prune()
    assert len(store.list()) == 1 and store.inspect(good.path).valid


def test_prune_failure_does_not_fail_new_snapshot(connection, database_path, monkeypatch):
    store = BackupStore(database_path, retain=1)
    old = store.create(connection, "daily", now=NOW)
    real_unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == old.path:
            raise OSError("Cannot prune")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    new = store.create(connection, "daily", now=NOW + timedelta(days=1))
    assert new.valid and old.path.exists() and "could not be removed" in store.warning


@pytest.mark.parametrize("failure", ["copy", "validation"])
def test_failed_backup_leaves_valid_history_and_no_temporary_artifacts(connection, database_path, monkeypatch, failure):
    store = BackupStore(database_path, retain=1)
    good = store.create(connection, "daily", now=NOW)
    before = set(store.directory.iterdir())

    def fail(*args, **kwargs):
        raise BackupError("Injected failure")

    monkeypatch.setattr(backups, "copy_sqlite" if failure == "copy" else "validate_snapshot", fail)
    with pytest.raises(BackupError):
        store.create(connection, "daily", now=NOW + timedelta(days=1))
    assert set(store.directory.iterdir()) == before
    assert good.path.exists()


def test_corruption_checksum_and_metadata_reconstruction(connection, database_path):
    store = BackupStore(database_path)
    info = store.create(connection, now=NOW)
    sidecar = info.path.with_suffix(".json")
    sidecar.write_text("{damaged index")
    rebuilt = store.inspect(info.path)
    assert rebuilt.valid and rebuilt == info
    sidecar.write_text(json.dumps({"sha256": "wrong"}))
    assert "checksum" in store.inspect(info.path).error
    sidecar.unlink()
    info.path.write_bytes(b"not SQLite")
    assert not store.inspect(info.path).valid
    assert not store.list()[0].valid


def test_malformed_catalog_timestamp_is_shown_as_invalid(connection, database_path):
    store = BackupStore(database_path)
    info = store.create(connection)
    malformed = info.path.with_name('20261399T999999999999Z' + info.path.name[22:])
    info.path.rename(malformed)
    catalog = store.list()
    assert len(catalog) == 1 and not catalog[0].valid and "timestamp" in catalog[0].error


def test_snapshot_with_missing_schema_or_orphan_children_is_rejected(connection, repository, database_path, full_episode):
    repository.create(full_episode)
    store = BackupStore(database_path)
    snapshot = store.create(connection)
    snapshot.path.with_suffix('.json').unlink()
    with closing(sqlite3.connect(snapshot.path, isolation_level=None)) as broken:
        broken.execute("INSERT INTO episode_symptoms VALUES ('missing episode', 0, 'pain')")
    assert not store.inspect(snapshot.path).valid
    with closing(sqlite3.connect(snapshot.path, isolation_level=None)) as broken:
        broken.execute("DELETE FROM episode_symptoms WHERE episode_id = 'missing episode'")
        broken.execute("DROP TABLE episode_adaptations")
    assert not store.inspect(snapshot.path).valid


def test_metadata_write_failure_keeps_restorable_snapshot(connection, database_path, monkeypatch):
    store = BackupStore(database_path)

    def fail(*args):
        raise OSError('Metadata unavailable')

    monkeypatch.setattr(store, '_metadata', fail)
    info = store.create(connection)
    assert info.valid and store.inspect(info.path).valid
    assert 'metadata' in store.warning


def test_failed_atomic_placement_keeps_previous_history(connection, database_path, monkeypatch):
    store = BackupStore(database_path)
    good = store.create(connection)
    before = set(store.directory.iterdir())

    def fail(*args):
        raise OSError('Cannot place snapshot')

    monkeypatch.setattr(backups.os, 'replace', fail)
    with pytest.raises(BackupError):
        store.create(connection)
    assert set(store.directory.iterdir()) == before and store.inspect(good.path).valid


def test_backup_paths_survive_cwd_change(connection, database_path, tmp_path, monkeypatch):
    store = BackupStore(database_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    info = store.create(connection)
    assert info.path.parent == database_path.parent / "backups"
    assert list(elsewhere.iterdir()) == []


def test_active_transaction_is_rejected_instead_of_hanging(connection, database_path):
    connection.execute("BEGIN")
    try:
        with pytest.raises(BackupError, match="transaction"):
            BackupStore(database_path).create(connection)
        with pytest.raises(RuntimeError, match="transaction"):
            migrations.apply_migrations(connection)
        assert connection.in_transaction
    finally:
        connection.rollback()


@pytest.mark.parametrize("kind", ["daily", "manual", "migration"])
def test_naive_backup_time_remains_rejected_without_writes(connection, database_path, kind):
    store = BackupStore(database_path)
    before = connection.total_changes
    with pytest.raises(ValueError, match="Backup time must have a timezone"):
        store.create(connection, kind, now=datetime(2026, 10, 25, 12))
    assert connection.total_changes == before
    assert not store.directory.exists()


def test_interrupted_sqlite_copy_rolls_back_destination(tmp_path, full_episode):
    with closing(connect_database(tmp_path / "source.sqlite3")) as source, closing(connect_database(tmp_path / "target.sqlite3")) as target:
        full_episode.notes = "large snapshot " * 400_000
        EpisodeRepository(source).create(full_episode)
        with pytest.raises(BackupError, match="busy"):
            copy_sqlite(source, target, timeout=0)
        assert EpisodeRepository(target).list() == []
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_upgrade_creates_verified_snapshot_before_migrating(database_path, full_episode, monkeypatch):
    with closing(connect_database(database_path)) as original:
        EpisodeRepository(original).create(full_episode)
    upgrade = migrations.Migration(2, "test_upgrade", ("CREATE TABLE upgrade_marker (value TEXT)",))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, upgrade))
    with closing(connect_database(database_path)) as upgraded:
        assert migrations.schema_version(upgraded) == 2
    snapshots = BackupStore(database_path).list()
    assert len(snapshots) == 1 and snapshots[0].kind == "migration"
    assert snapshots[0].schema_version == 1 and snapshots[0].valid
    assert contents(snapshots[0].path) == [full_episode]


def test_upgrade_aborts_if_required_backup_fails(database_path, full_episode, monkeypatch):
    with closing(connect_database(database_path)) as original:
        EpisodeRepository(original).create(full_episode)
    upgrade = migrations.Migration(2, "test_upgrade", ("DELETE FROM episodes",))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, upgrade))

    def fail(*args, **kwargs):
        raise BackupError("Required backup failed")

    monkeypatch.setattr(BackupStore, "create", fail)
    with pytest.raises(BackupError, match="Required backup"):
        connect_database(database_path)
    with closing(readonly(database_path)) as unchanged:
        unchanged.row_factory = sqlite3.Row
        assert migrations.schema_version(unchanged) == 1
        assert EpisodeRepository(unchanged).list() == [full_episode]
