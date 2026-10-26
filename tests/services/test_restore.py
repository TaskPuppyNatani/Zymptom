from copy import deepcopy
from contextlib import closing
import sqlite3

import pytest

from zymptom.domain import EpisodeOutcome
from zymptom.persistence import migrations
from zymptom.persistence.backups import BackupError, readonly
from zymptom.services.journal import JournalSession
from zymptom.services import journal as journal_module


@pytest.fixture
def journal(database_path):
    session = JournalSession(database_path)
    yield session
    session.close()


def old_and_current(journal, full_episode):
    journal.episodes.repository.create(full_episode)
    original = deepcopy(full_episode)
    snapshot = journal.backups.create(journal.connection)
    full_episode.activity = "Current journal café ☕"
    full_episode.notes = "Current text\n雪"
    journal.episodes.update(full_episode)
    journal.episodes.record(activity="Only in current", outcome=EpisodeOutcome.STOPPED_EARLY)
    return original, snapshot, journal.episodes.list()


def test_restore_returns_exact_contents_and_reopens_existing_service(journal, full_episode):
    original, snapshot, current = old_and_current(journal, full_episode)
    service = journal.episodes
    old_connection = journal.connection
    plan = journal.prepare_restore(snapshot.path)
    assert plan.safety.valid and plan.safety.kind == "pre-restore"
    assert journal.episodes.list() == current
    journal.restore(plan)
    assert journal.episodes is service
    assert service.list() == [original]
    assert journal.connection is not old_connection
    with pytest.raises(sqlite3.ProgrammingError):
        old_connection.execute("SELECT 1")
    assert plan.safety.path.exists() and journal.backups.inspect(plan.safety.path).valid
    assert not plan.staged.exists()
    with closing(readonly(plan.safety.path)) as saved:
        saved.row_factory = sqlite3.Row
        from zymptom.persistence import EpisodeRepository
        assert EpisodeRepository(saved).list() == current


def test_cancel_preserves_journal_and_safety_snapshot(journal, full_episode):
    _, snapshot, current = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    journal.cancel_restore()
    assert journal.episodes.list() == current
    assert plan.safety.path.exists() and not plan.staged.exists()
    with pytest.raises(BackupError, match="confirm"):
        journal.restore(plan)


def test_post_restore_reopen_failure_recovers_original_database(journal, full_episode, monkeypatch):
    _, snapshot, current = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    real_reopen = journal._reopen
    calls = []

    def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("Injected reopen failure after replacement")
        real_reopen()

    monkeypatch.setattr(journal, "_reopen", fail_once)
    with pytest.raises(BackupError, match="previous working journal was recovered"):
        journal.restore(plan)
    assert journal.episodes.list() == current
    assert journal.backups.inspect(plan.safety.path).valid
    assert not plan.staged.exists()
    journal.episodes.record(activity="Still usable", outcome=EpisodeOutcome.COMPLETED_NORMALLY)


def test_recovery_after_actual_connection_reopen_failure(journal, full_episode, monkeypatch):
    _, snapshot, current = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    connect = journal_module.connect_database
    attempts = []

    def fail_once(path):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError('Reopen failed after old connection closed')
        return connect(path)

    monkeypatch.setattr(journal_module, 'connect_database', fail_once)
    with pytest.raises(BackupError, match='previous working journal was recovered'):
        journal.restore(plan)
    assert journal.episodes.list() == current and journal.backups.inspect(plan.safety.path).valid


def test_recovery_failure_preserves_safety_and_disables_closed_session(journal, full_episode, monkeypatch):
    _, snapshot, _ = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    copy = journal_module.copy_sqlite
    attempts = []

    def fail_after_first_copy(source, destination):
        attempts.append(True)
        if len(attempts) == 1:
            copy(source, destination)
        raise OSError('Simulated storage failure')

    monkeypatch.setattr(journal_module, 'copy_sqlite', fail_after_first_copy)
    with pytest.raises(BackupError, match='safety backup is preserved'):
        journal.restore(plan)
    assert journal.connection is None
    assert journal.backups.inspect(plan.safety.path).valid
    assert not plan.staged.exists()
    with pytest.raises(BackupError, match='unavailable'):
        journal.prepare_restore(snapshot.path)


def test_changed_journal_or_staging_is_refused_without_overwriting(journal, full_episode):
    _, snapshot, _ = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    journal.episodes.record(activity="New after confirmation opened", outcome=EpisodeOutcome.STOPPED_EARLY)
    current = journal.episodes.list()
    with pytest.raises(BackupError, match="journal changed"):
        journal.restore(plan)
    assert journal.episodes.list() == current
    plan = journal.prepare_restore(snapshot.path)
    plan.staged.write_bytes(b"corrupted staging")
    with pytest.raises(BackupError, match="snapshot changed"):
        journal.restore(plan)
    assert journal.episodes.list() == current


def test_safety_snapshot_is_reverified_after_confirmation_before_any_live_write(journal, full_episode):
    _, snapshot, current = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    plan.safety.path.write_bytes(b'broken safety backup')
    with pytest.raises(BackupError, match='safety backup changed'):
        journal.restore(plan)
    assert journal.episodes.list() == current
    assert not plan.staged.exists()


@pytest.mark.parametrize("kind", ["corrupt", "newer", "wrong_history"])
def test_invalid_snapshots_refused_before_safety_or_live_changes(journal, full_episode, kind):
    _, snapshot, current = old_and_current(journal, full_episode)
    if kind == "corrupt":
        snapshot.path.write_bytes(b"broken backup")
    else:
        with closing(sqlite3.connect(snapshot.path, isolation_level=None)) as connection:
            if kind == "newer":
                connection.execute("INSERT INTO schema_migrations VALUES (99, 'future', '2100-01-01')")
            else:
                connection.execute("UPDATE schema_migrations SET name = 'other app'")
        snapshot.path.with_suffix(".json").unlink()
    before = set(journal.backups.directory.iterdir())
    with pytest.raises(BackupError, match="newer schema" if kind == "newer" else "."):
        journal.prepare_restore(snapshot.path)
    assert journal.episodes.list() == current
    assert set(journal.backups.directory.iterdir()) == before


def test_supported_older_snapshot_upgrades_in_staging(journal, full_episode, monkeypatch):
    journal.episodes.repository.create(full_episode)
    original = deepcopy(full_episode)
    snapshot = journal.backups.create(journal.connection)
    current_version = migrations.MIGRATIONS[-1].version
    upgrade = migrations.Migration(current_version + 1, "test_upgrade", ("CREATE TABLE upgrade_marker (value TEXT)",))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, upgrade))
    journal._reopen()
    journal.episodes.delete(full_episode.id)
    plan = journal.prepare_restore(snapshot.path)
    assert journal.backups.inspect(snapshot.path).schema_version == current_version
    journal.restore(plan)
    assert migrations.schema_version(journal.connection) == current_version + 1
    assert journal.episodes.list() == [original]


def test_future_destructive_hook_creates_verified_safety_snapshot(journal):
    info = journal.before_destructive_change()
    assert info.valid and info.kind == "safety"


@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_restore_holds_exclusive_lock_for_final_safety_check_and_transfer(journal, full_episode, monkeypatch, mode):
    journal.connection.execute(f"PRAGMA journal_mode = {mode}")
    original, snapshot, _ = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    copy = journal_module.copy_sqlite
    checked = []

    def assert_locked(source, destination):
        with closing(sqlite3.connect(journal.database, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        checked.append(True)
        return copy(source, destination)

    monkeypatch.setattr(journal_module, "copy_sqlite", assert_locked)
    journal.restore(plan)
    assert checked and journal.episodes.list() == [original]
    assert journal.connection.execute("PRAGMA locking_mode").fetchone()[0] == "normal"


def test_busy_journal_restore_is_refused_and_releases_requested_lock(journal, full_episode):
    _, snapshot, current = old_and_current(journal, full_episode)
    plan = journal.prepare_restore(snapshot.path)
    journal.connection.execute("PRAGMA busy_timeout = 1")
    with closing(sqlite3.connect(journal.database, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(BackupError, match="locked"):
            journal.restore(plan)
        writer.rollback()
    assert journal.episodes.list() == current
    assert plan.safety.path.exists() and journal.connection.execute("PRAGMA locking_mode").fetchone()[0] == "normal"
