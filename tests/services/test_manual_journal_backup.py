from contextlib import closing
from copy import deepcopy
import os

import pytest

from zymptom.persistence.backups import content_hash, readonly
from zymptom.services.journal import JournalSession, ManualBackupError, RecoveryError
from zymptom.domain import EpisodeOutcome
from zymptom.sync.simulator import LocalSimulator


def test_full_backup_is_recoverable_with_complete_enrolled_state(tmp_path, full_episode):
    source = JournalSession(tmp_path / "source.sqlite3")
    target = JournalSession(tmp_path / "target.sqlite3")
    try:
        source.connection.execute("PRAGMA journal_mode=WAL")
        episode = deepcopy(full_episode)
        episode.duration_minutes = 0
        episode.recovery_required = False
        episode.recovery_minutes = 0
        episode.help_notes = None
        episode.notes = "Café ☕\n雪"
        source.episodes.repository.create(episode)
        enrollment = source.enroll_sync()
        change = source.connection.execute("SELECT change_id FROM sync_changes LIMIT 1").fetchone()[0]
        source.connection.execute("INSERT INTO sync_acknowledgements VALUES (?, ?)", ("firebase-production:test", change))
        before = content_hash(source.connection)
        original_entries = source.episodes.list()
        internal = {info.path for info in source.backups.list()}
        path = tmp_path / "chosen.sqlite3"
        assert source.create_manual_backup(path) == 1
        assert content_hash(source.connection) == before
        assert {info.path for info in source.backups.list()} == internal
        preview = target.inspect_recovery(path)
        assert preview.entry_count == 1 and preview.content_hash == before
        with closing(readonly(path)) as snapshot:
            assert content_hash(snapshot) == before
        target.restore(target.prepare_recovery(preview))
        assert target.episodes.list() == original_entries
        assert target.inspect_enrollment().journal_id == enrollment.journal_id
        assert content_hash(target.connection) == before
        assert target.connection.execute("SELECT count(*) FROM sync_acknowledgements").fetchone()[0] == 1
        source.backups.prune()
        assert path.exists()
    finally:
        source.close()
        target.close()


def test_current_database_aliases_and_sidecars_are_rejected(tmp_path):
    session = JournalSession(tmp_path / "current.sqlite3")
    try:
        link = tmp_path / "link.sqlite3"
        link.symlink_to(session.database)
        hardlink = tmp_path / "hard.sqlite3"
        os.link(session.database, hardlink)
        for path in (session.database, link, hardlink, tmp_path / "current.sqlite3-wal"):
            with pytest.raises(ManualBackupError) as caught:
                session.create_manual_backup(path, overwrite=True)
            assert caught.value.kind == "current"
        assert session.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        session.close()


@pytest.mark.parametrize("failure", ["inspection", "copy", "replace"])
def test_failure_preserves_existing_destination_and_cleans_staging(tmp_path, monkeypatch, failure):
    from zymptom.services import journal as module
    session = JournalSession(tmp_path / "current.sqlite3")
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"existing contents")
    try:
        before = content_hash(session.connection)
        def fail(*args, **kwargs):
            if failure == "inspection":
                raise RecoveryError("private diagnostic")
            raise OSError("private diagnostic")
        if failure == "inspection":
            monkeypatch.setattr(session, "inspect_recovery", fail)
        else:
            monkeypatch.setattr(module if failure == "copy" else module.os, "copy_sqlite" if failure == "copy" else "replace", fail)
        with pytest.raises(ManualBackupError) as caught:
            session.create_manual_backup(destination, overwrite=True)
        assert caught.value.kind == ("verify" if failure == "inspection" else "write")
        assert destination.read_bytes() == b"existing contents"
        assert content_hash(session.connection) == before
        assert not list(tmp_path.glob(".zymptom-journal-*.tmp"))
    finally:
        session.close()


def test_existing_destination_needs_overwrite_and_sidecars_are_preserved(tmp_path):
    session = JournalSession(tmp_path / "current.sqlite3")
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"keep")
    try:
        with pytest.raises(ManualBackupError) as caught:
            session.create_manual_backup(destination)
        assert caught.value.kind == "exists" and destination.read_bytes() == b"keep"
        sidecar = tmp_path / "existing.sqlite3-wal"
        sidecar.write_bytes(b"keep sidecar")
        with pytest.raises(ManualBackupError):
            session.create_manual_backup(destination, overwrite=True)
        assert destination.read_bytes() == b"keep" and sidecar.read_bytes() == b"keep sidecar"
        sidecar.unlink()
        session.create_manual_backup(destination, overwrite=True)
        assert session.inspect_recovery(destination).entry_count == 0
    finally:
        session.close()


def test_unresolved_conflicts_survive_backup_and_recovery(tmp_path):
    simulator = LocalSimulator(tmp_path / "devices")
    target = JournalSession(tmp_path / "target.sqlite3")
    try:
        episode = simulator.desktop.episodes.record(activity="Shared", outcome=EpisodeOutcome.STOPPED_EARLY)
        simulator.exchange()
        for device, notes in ((simulator.desktop, "Desktop"), (simulator.phone, "Phone")):
            draft = device.episodes.get(episode.id)
            draft.notes = notes
            device.episodes.update(draft)
        simulator.exchange()
        source = simulator.desktop.session
        assert source.connection.execute("SELECT count(*) FROM sync_conflicts").fetchone()[0] == 1
        expected = content_hash(source.connection)
        path = tmp_path / "conflicted.sqlite3"
        source.create_manual_backup(path)
        target.restore(target.prepare_recovery(target.inspect_recovery(path)))
        assert content_hash(target.connection) == expected
        assert target.connection.execute("SELECT count(*) FROM sync_conflicts").fetchone()[0] == 1
    finally:
        target.close()
        simulator.close()


def test_new_destination_is_not_overwritten_if_created_during_snapshot(tmp_path, monkeypatch):
    from zymptom.services import journal as module
    session = JournalSession(tmp_path / "current.sqlite3")
    destination = tmp_path / "race.sqlite3"
    real_copy = module.copy_sqlite
    def race(source, target):
        real_copy(source, target)
        destination.write_bytes(b"someone else's file")
    monkeypatch.setattr(module, "copy_sqlite", race)
    try:
        with pytest.raises(ManualBackupError):
            session.create_manual_backup(destination)
        assert destination.read_bytes() == b"someone else's file"
        assert not list(tmp_path.glob(".zymptom-journal-*.tmp"))
    finally:
        session.close()


def test_confirmed_destination_changed_during_snapshot_is_not_replaced(tmp_path, monkeypatch):
    from zymptom.services import journal as module
    session = JournalSession(tmp_path / "current.sqlite3")
    destination = tmp_path / "confirmed.sqlite3"
    destination.write_bytes(b"original")
    real_copy = module.copy_sqlite
    def race(source, target):
        real_copy(source, target)
        destination.write_bytes(b"changed by another process")
    monkeypatch.setattr(module, "copy_sqlite", race)
    try:
        with pytest.raises(ManualBackupError) as caught:
            session.create_manual_backup(destination, overwrite=True)
        assert caught.value.kind == "exists"
        assert destination.read_bytes() == b"changed by another process"
    finally:
        session.close()
