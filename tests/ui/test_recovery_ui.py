from contextlib import closing
from copy import deepcopy

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence.backups import BackupError, file_checksum
from zymptom.services.journal import JournalSession, RecoveryError
from zymptom.ui.sandbox_app import SandboxWindow
from zymptom.ui.window import MainWindow


def test_choose_preview_cancel_recover_refresh_and_restart(qapp, zone, tmp_path, full_episode, monkeypatch):
    current = JournalSession(tmp_path / "current.sqlite3")
    source = tmp_path / "selected.sqlite3"
    episode = deepcopy(full_episode)
    with closing(connect_database(source)) as connection:
        EpisodeRepository(connection).create(episode)
    before = file_checksum(source)
    window = MainWindow(current.episodes, zone=zone, now=lambda: episode.occurred_at, backup_session=current)
    window.show()
    qapp.processEvents()
    try:
        assert window.recovery_action.isEnabled()
        window.recovery_action.trigger()
        dialog = window.recovery_dialog
        assert dialog is not None and not dialog.recover_button.isEnabled()
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(source), ""))
        dialog.choose_button.click()
        assert "Valid Zymptom journal" in dialog.details.text()
        assert "Entries: 1" in dialog.details.text()
        assert "Journal version: 2" in dialog.details.text()
        assert "No upgrade required" in dialog.details.text()
        assert dialog.recover_button.isEnabled()
        backups_before = len(current.backups.list())
        dialog.recover_button.click()
        assert dialog.confirmation is not None
        dialog.confirmation.button(QMessageBox.StandardButton.Cancel).click()
        assert current.episodes.list() == []
        assert len(current.backups.list()) == backups_before
        dialog.recover_button.click()
        dialog.confirmation.button(QMessageBox.StandardButton.Yes).click()
        qapp.processEvents()
        assert "Journal recovered successfully" in dialog.status.text()
        assert current.episodes.list() == [episode]
        assert window.entries.count() == 1
        assert any(episode.activity in window.history.entries.item(i).text() for i in range(window.history.entries.count()))
        assert any(info.valid and info.kind == "pre-restore" for info in current.backups.list())
        assert file_checksum(source) == before
        dialog.reject()
        window.close()
        current.close()
        current = JournalSession(tmp_path / "current.sqlite3")
        assert current.episodes.list() == [episode]
    finally:
        if window.recovery_dialog:
            window.recovery_dialog.reject()
        window.close()
        current.close()


def test_invalid_source_and_sandbox_disabled(qapp, zone, tmp_path):
    session = JournalSession(tmp_path / "sandbox.sqlite3")
    normal = MainWindow(session.episodes, zone=zone, backup_session=session)
    sandbox = SandboxWindow(session, tmp_path / "production.sqlite3")
    invalid = tmp_path / "invalid.txt"
    invalid.write_text("not a journal")
    try:
        normal.open_recovery_dialog()
        dialog = normal.recovery_dialog
        dialog.inspect_file(invalid)
        assert not dialog.recover_button.isEnabled()
        assert "valid Zymptom journal" in dialog.status.text()
        assert not sandbox.recovery_action.isEnabled()
        sandbox.open_recovery_dialog()
        assert sandbox.recovery_dialog is None
        assert "disabled" in sandbox.statusBar().currentMessage()
        assert session.episodes.list() == []
    finally:
        if normal.recovery_dialog:
            normal.recovery_dialog.reject()
        normal.close()
        sandbox.close()
        session.close()


@pytest.mark.parametrize("kind,text", [("backup", "safety backup"), ("migration", "could not upgrade")])
def test_failure_message_is_safe_and_current_journal_remains_open(qapp, zone, tmp_path, full_episode, monkeypatch, kind, text):
    session = JournalSession(tmp_path / "current.sqlite3")
    source = tmp_path / "source.sqlite3"
    with closing(connect_database(source)) as connection:
        EpisodeRepository(connection).create(deepcopy(full_episode))
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    try:
        window.open_recovery_dialog()
        dialog = window.recovery_dialog
        dialog.inspect_file(source)
        def fail(_preview):
            raise RecoveryError("private raw details", kind=kind)
        monkeypatch.setattr(session, "prepare_recovery", fail)
        dialog.confirm_recovery()
        dialog.confirmation.button(QMessageBox.StandardButton.Yes).click()
        assert text in dialog.status.text()
        assert "private raw details" not in dialog.status.text()
        assert dialog.recover_button.isEnabled()
        assert session.connection is not None and session.episodes.list() == []
    finally:
        if window.recovery_dialog:
            window.recovery_dialog.reject()
        window.close()
        session.close()


def test_unrecoverable_failure_shows_safety_backup_location(qapp, zone, tmp_path, full_episode, monkeypatch):
    session = JournalSession(tmp_path / "current.sqlite3")
    source = tmp_path / "source.sqlite3"
    with closing(connect_database(source)) as connection:
        EpisodeRepository(connection).create(deepcopy(full_episode))
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    connection = session.connection
    try:
        window.open_recovery_dialog()
        dialog = window.recovery_dialog
        dialog.inspect_file(source)
        def fail(_plan):
            session.connection = None
            raise BackupError("private raw exception")
        monkeypatch.setattr(session, "restore", fail)
        dialog.confirm_recovery()
        dialog.confirmation.button(QMessageBox.StandardButton.Yes).click()
        assert "safety backup is preserved at:" in dialog.status.text()
        assert str(session.backups.directory) in dialog.status.text()
        assert "private raw exception" not in dialog.status.text()
        assert not dialog.recover_button.isEnabled()
    finally:
        session.connection = connection
        if window.recovery_dialog:
            window.recovery_dialog.reject()
        window.close()
        session.close()
