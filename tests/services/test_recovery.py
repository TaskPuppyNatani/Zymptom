from contextlib import closing
from copy import deepcopy
import os
import sqlite3

import pytest

from zymptom.domain import EpisodeOutcome
from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence.backups import BackupError, file_checksum, readonly, validate_connection
from zymptom.persistence.database import transaction
from zymptom.persistence.migrations import MIGRATION_001, apply_migrations, schema_version
from zymptom.services import journal as journal_module
from zymptom.services.journal import JournalSession, RecoveryError


@pytest.fixture
def journal(tmp_path):
    session = JournalSession(tmp_path / "current.sqlite3")
    yield session
    session.close()


def source_file(path, episode, *, older=False):
    if older:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        apply_migrations(connection, (MIGRATION_001,))
        with transaction(connection):
            EpisodeRepository(connection)._apply_sync_snapshot(episode.id, episode)
    else:
        connection = connect_database(path)
        EpisodeRepository(connection).create(deepcopy(episode))
    connection.close()


@pytest.mark.parametrize("older", [False, True])
def test_inspection_preserves_source_and_reports_preview(journal, tmp_path, full_episode, older):
    path = tmp_path / "external.sqlite3"
    source_file(path, full_episode, older=older)
    before = file_checksum(path)
    preview = journal.inspect_recovery(path)
    assert preview.entry_count == 1
    assert preview.oldest == preview.newest == full_episode.occurred_at
    assert preview.schema_version == (1 if older else 2)
    assert preview.migration_required is older
    assert file_checksum(path) == before
    assert sorted(item.name for item in tmp_path.iterdir() if item.name.startswith("external")) == ["external.sqlite3"]


@pytest.mark.parametrize("kind", ["not_sqlite", "arbitrary", "corrupt", "future", "incomplete", "forged_schema", "extra_schema"])
def test_invalid_sources_are_rejected_without_changing_current(journal, tmp_path, full_episode, kind):
    journal.episodes.record(activity="Keep current", outcome=EpisodeOutcome.STOPPED_EARLY)
    before = journal.episodes.list()
    path = tmp_path / "bad.sqlite3"
    if kind in {"not_sqlite", "corrupt"}:
        path.write_bytes(b"not sqlite" if kind == "not_sqlite" else b"SQLite format 3\x00" + b"\xff" * 256)
    elif kind == "arbitrary":
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE unrelated(value TEXT)")
    else:
        if kind in {"forged_schema", "extra_schema"}:
            with closing(connect_database(path)):
                pass
        else:
            source_file(path, full_episode)
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            if kind == "future":
                conn.execute("INSERT INTO schema_migrations VALUES(99,'future','2100')")
            elif kind == "incomplete":
                conn.execute("DROP TABLE episode_symptoms")
            elif kind == "extra_schema":
                conn.execute("CREATE TABLE sqliteXshadow (value TEXT)")
            else:
                conn.execute("DROP TABLE episode_symptoms")
                conn.execute("CREATE TABLE episode_symptoms (episode_id TEXT NOT NULL, position INTEGER NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (episode_id, position))")
    if kind in {"forged_schema", "extra_schema"}:
        with closing(journal._readonly_recovery(path)) as connection:
            assert validate_connection(connection)[0] == 2
    checksum = file_checksum(path)
    with pytest.raises(RecoveryError) as caught:
        journal.inspect_recovery(path)
    if kind == "future":
        assert caught.value.kind == "newer"
    assert file_checksum(path) == checksum
    assert journal.episodes.list() == before


def test_active_database_symlink_hardlink_and_sidecar_are_rejected(journal, tmp_path, full_episode):
    with pytest.raises(RecoveryError):
        journal.inspect_recovery(tmp_path / "missing.sqlite3")
    with pytest.raises(RecoveryError, match="different"):
        journal.inspect_recovery(journal.database)
    symlink = tmp_path / "link.sqlite3"
    symlink.symlink_to(journal.database)
    hardlink = tmp_path / "hardlink.sqlite3"
    os.link(journal.database, hardlink)
    for alias in (symlink, hardlink):
        with pytest.raises(RecoveryError, match="different"):
            journal.inspect_recovery(alias)
    path = tmp_path / "source.sqlite3"
    source_file(path, full_episode)
    sidecar = tmp_path / "source.sqlite3-wal"
    sidecar.write_bytes(b"do not touch")
    before = file_checksum(path)
    with pytest.raises(RecoveryError) as caught:
        journal.inspect_recovery(path)
    assert caught.value.kind == "open"
    assert file_checksum(path) == before and sidecar.read_bytes() == b"do not touch"


def test_sandbox_mailbox_acknowledgements_are_not_imported(journal, tmp_path, full_episode):
    path = tmp_path / "sandbox-source.sqlite3"
    source_file(path, full_episode)
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        change_id = connection.execute("SELECT change_id FROM sync_changes").fetchone()[0]
        connection.execute("INSERT INTO sync_acknowledgements VALUES (?, ?)", ("firebase-sandbox:opaque", change_id))
    before = file_checksum(path)
    with pytest.raises(RecoveryError) as caught:
        journal.inspect_recovery(path)
    assert caught.value.kind == "sandbox"
    assert journal.episodes.list() == [] and file_checksum(path) == before


@pytest.mark.parametrize("older", [False, True])
def test_recovery_backups_before_activation_migrates_copy_and_reopens(journal, tmp_path, full_episode, older):
    journal.episodes.record(activity="Previous journal", outcome=EpisodeOutcome.STOPPED_EARLY)
    previous = journal.episodes.list()
    path = tmp_path / "external.sqlite3"
    source_file(path, full_episode, older=older)
    before = file_checksum(path)
    preview = journal.inspect_recovery(path)
    service = journal.episodes
    device_id = service.repository.device_id
    plan = journal.prepare_recovery(preview)
    assert plan.safety.valid and journal.backups.inspect(plan.safety.path).valid
    assert journal.episodes.list() == previous
    with closing(readonly(plan.safety.path)) as backup:
        backup.row_factory = sqlite3.Row
        assert EpisodeRepository(backup).list() == previous
    journal.restore(plan)
    assert journal.episodes is service
    assert journal.episodes.repository.device_id == device_id
    expected = deepcopy(full_episode)
    if not older:
        for field in ("functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions"):
            setattr(expected, field, list(dict.fromkeys(getattr(expected, field))))
    assert journal.episodes.list() == [expected]
    assert schema_version(journal.connection) == 2
    assert file_checksum(path) == before
    with closing(sqlite3.connect(path)) as original:
        assert schema_version(original) == (1 if older else 2)


def test_changed_source_backup_failure_and_migration_failure_leave_current_untouched(journal, tmp_path, full_episode, monkeypatch):
    journal.episodes.record(activity="Keep current", outcome=EpisodeOutcome.STOPPED_EARLY)
    before = journal.episodes.list()
    path = tmp_path / "external.sqlite3"
    source_file(path, full_episode)
    preview = journal.inspect_recovery(path)
    with closing(connect_database(path)) as connection:
        repo = EpisodeRepository(connection)
        changed = repo.get(full_episode.id)
        changed.activity = "Changed"
        repo.update(changed)
    with pytest.raises(RecoveryError) as caught:
        journal.prepare_recovery(preview)
    assert caught.value.kind == "changed"
    assert journal.episodes.list() == before
    source_file_path = tmp_path / "legacy.sqlite3"
    source_file(source_file_path, full_episode, older=True)
    preview = journal.inspect_recovery(source_file_path)
    with monkeypatch.context() as patch:
        patch.setattr(journal.backups, "create", lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("injected")))
        with pytest.raises(RecoveryError) as caught:
            journal.prepare_recovery(preview)
        assert caught.value.kind == "backup"
    with monkeypatch.context() as patch:
        migrate = journal_module.apply_migrations
        def fail_staged(connection, *args, **kwargs):
            if connection.execute("PRAGMA database_list").fetchone()[2]:
                raise RuntimeError("injected")
            return migrate(connection, *args, **kwargs)
        patch.setattr(journal_module, "apply_migrations", fail_staged)
        with pytest.raises(RecoveryError) as caught:
            journal.prepare_recovery(preview)
        assert caught.value.kind == "migration"
    assert journal.episodes.list() == before


def test_activation_failure_restores_safety_backup(journal, tmp_path, full_episode, monkeypatch):
    journal.episodes.record(activity="Keep current", outcome=EpisodeOutcome.STOPPED_EARLY)
    before = journal.episodes.list()
    path = tmp_path / "external.sqlite3"
    source_file(path, full_episode)
    plan = journal.prepare_recovery(journal.inspect_recovery(path))
    reopen = journal._reopen
    attempts = []
    def fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("injected")
        reopen()
    monkeypatch.setattr(journal, "_reopen", fail_once)
    with pytest.raises(BackupError):
        journal.restore(plan)
    assert journal.episodes.list() == before
    assert journal.backups.inspect(plan.safety.path).valid


def test_source_change_during_staging_aborts_before_backup_or_activation(journal, tmp_path, full_episode, monkeypatch):
    journal.episodes.record(activity="Keep current", outcome=EpisodeOutcome.STOPPED_EARLY)
    before = journal.episodes.list()
    path = tmp_path / "external.sqlite3"
    source_file(path, full_episode)
    preview = journal.inspect_recovery(path)
    copy = journal_module.copy_sqlite
    def change_after_copy(source, destination):
        copy(source, destination)
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            connection.execute("UPDATE episodes SET activity = 'Changed during staging'")
    monkeypatch.setattr(journal_module, "copy_sqlite", change_after_copy)
    with pytest.raises(RecoveryError) as caught:
        journal.prepare_recovery(preview)
    assert caught.value.kind == "stage"
    assert journal.episodes.list() == before
    assert not [item for item in journal.backups.list() if item.kind == "pre-restore"]
