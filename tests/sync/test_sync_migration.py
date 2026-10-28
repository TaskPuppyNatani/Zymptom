from contextlib import closing
from copy import deepcopy
import sqlite3

import pytest

from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence.backups import BackupError, BackupStore, content_hash, validate_snapshot
from zymptom.persistence.database import transaction
from zymptom.persistence.migrations import MIGRATION_001, apply_migrations, schema_version
from zymptom.services.journal import JournalSession
from zymptom.sync import storage
from zymptom.sync.identity import DeviceIdentity
from zymptom.sync.storage import get_head, read_changes, validate_state


def legacy_database(path, episode):
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(connection, (MIGRATION_001,))
        with transaction(connection):
            EpisodeRepository(connection)._apply_sync_snapshot(episode.id, episode)


def test_schema_one_upgrade_backfills_deterministic_roots_without_touching_records(tmp_path, full_episode):
    original = deepcopy(full_episode)
    ids = []
    for name in ("first.sqlite3", "second.sqlite3"):
        path = tmp_path / name
        legacy_database(path, original)
        with closing(connect_database(path)) as connection:
            assert schema_version(connection) == 3
            assert EpisodeRepository(connection).list() == [original]
            head = get_head(connection, original.id)
            assert head.revision == 1 and head.device_id.int == 0 and head.episode == original
            ids.append(head.change_id)
            validate_state(connection)
        snapshots = BackupStore(path).list()
        assert len(snapshots) == 1 and snapshots[0].valid and snapshots[0].schema_version == 1
    assert ids[0] == ids[1]
    assert full_episode == original


def test_failed_sync_data_initialization_rolls_back_schema_and_metadata(tmp_path, full_episode, monkeypatch):
    path = tmp_path / "old.sqlite3"
    legacy_database(path, full_episode)
    real_seed = storage.seed_legacy

    def fail(connection):
        real_seed(connection)
        raise RuntimeError("Injected migration initialization failure")

    monkeypatch.setattr(storage, "seed_legacy", fail)
    with pytest.raises(RuntimeError, match="initialization failure"):
        connect_database(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        assert schema_version(connection) == 1
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'sync_%'").fetchall()
        assert EpisodeRepository(connection).get(full_episode.id) == full_episode
    assert BackupStore(path).list()[0].valid


def test_backup_restore_preserves_sync_heads_tombstones_and_installation_identity(tmp_path, full_episode):
    session = JournalSession(tmp_path / "current.sqlite3")
    try:
        original_device = session.episodes.repository.device_id
        session.episodes.repository.create(full_episode)
        session.episodes.delete(full_episode.id)
        head = get_head(session.connection, full_episode.id)
        snapshot = session.backups.create(session.connection)
        before = content_hash(session.connection)
        from zymptom.domain import EpisodeOutcome
        session.episodes.record(activity="Discarded by explicit test restore", outcome=EpisodeOutcome.STOPPED_EARLY)
        session.restore(session.prepare_restore(snapshot.path))
        assert get_head(session.connection, full_episode.id) == head
        assert head.deleted and session.episodes.list() == []
        assert content_hash(session.connection) == before
        assert session.episodes.repository.device_id == original_device == DeviceIdentity().get()
    finally:
        session.close()


def test_backup_refuses_inconsistent_sync_state(repository, connection, database_path, full_episode):
    repository.create(full_episode)
    snapshot = BackupStore(database_path).create(connection)
    with closing(sqlite3.connect(snapshot.path, isolation_level=None)) as corrupted:
        corrupted.execute("UPDATE episodes SET activity = 'Untracked modification'")
    with pytest.raises(BackupError, match="valid Zymptom"):
        validate_snapshot(snapshot.path)
