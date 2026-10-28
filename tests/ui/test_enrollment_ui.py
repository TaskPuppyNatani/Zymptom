from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QMessageBox

from zymptom.domain import EpisodeOutcome
from zymptom.persistence import EpisodeRepository
from zymptom.persistence.backups import BackupError, content_hash, file_checksum
from zymptom.persistence.database import transaction
from zymptom.services.journal import JournalSession
from zymptom.sync.contract import SyncError
from zymptom.sync.storage import read_changes, read_enrollment
from zymptom.ui.sandbox_app import SandboxWindow
from zymptom.ui.window import MainWindow


def insert_untracked(connection, episode):
    with transaction(connection):
        EpisodeRepository(connection)._apply_sync_snapshot(episode.id, episode)
    return episode


def test_settings_sync_preview_cancel_confirm_and_restart(qapp, zone, tmp_path, full_episode):
    database = tmp_path / "production.sqlite3"
    session = JournalSession(database)
    insert_untracked(session.connection, deepcopy(full_episode))
    insert_untracked(
        session.connection,
        replace(deepcopy(full_episode), id=uuid4(), activity="Second pre-sync entry"),
    )
    tracked = session.episodes.record(activity="Already tracked entry", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
    expected_episodes = session.episodes.list()
    before_hash = content_hash(session.connection)
    before_checksum = file_checksum(database)

    window = MainWindow(session.episodes, zone=zone, now=lambda: full_episode.occurred_at, backup_session=session)
    window.show()
    qapp.processEvents()
    try:
        assert window.sync_action.isEnabled()
        backups_before = len(session.backups.list())

        window.sync_action.trigger()
        dialog = window.enrollment_dialog
        assert dialog is not None
        assert dialog.state_label.text() == "Sync not set up"
        assert "Nothing will be uploaded yet" in dialog.summary_label.text()
        assert "Not yet prepared" in dialog.details.text()
        assert "Existing journal entries: 3" in dialog.details.text()
        assert "Entries already carrying sync history: 1" in dialog.details.text()
        assert "Entries to receive bootstrap history: 2" in dialog.details.text()
        assert "Will be assigned during setup" in dialog.details.text()
        assert "Required before setup" in dialog.details.text()
        assert str(full_episode.id) not in dialog.details.text()
        assert str(tracked.id) not in dialog.details.text()
        assert dialog.prepare_button.isEnabled()
        assert content_hash(session.connection) == before_hash
        assert file_checksum(database) == before_checksum
        assert len(session.backups.list()) == backups_before

        dialog.prepare_button.click()
        assert dialog.confirmation is not None
        assert dialog.confirmation.defaultButton() == dialog.confirmation.button(QMessageBox.StandardButton.Cancel)
        text = dialog.confirmation.text()
        assert "Existing entries will be preserved" in text
        assert "Local sync history will be created" in text
        assert "safety backup will be created first" in text
        assert "Nothing will be uploaded yet" in text
        dialog.confirmation.button(QMessageBox.StandardButton.Cancel).click()
        assert "cancelled" in dialog.status.text()
        assert dialog.state_label.text() == "Sync not set up"
        assert dialog.prepare_button.isEnabled()
        assert content_hash(session.connection) == before_hash
        assert len(session.backups.list()) == backups_before
        dialog.reject()
        qapp.processEvents()
        assert window.enrollment_dialog is None

        window.sync_action.trigger()
        dialog = window.enrollment_dialog
        assert dialog is not None and dialog.state_label.text() == "Sync not set up"
        dialog.prepare_button.click()
        dialog.confirmation.button(QMessageBox.StandardButton.Yes).click()
        qapp.processEvents()

        assert dialog.state_label.text() == "Journal ready for sync"
        assert dialog.summary_label.text() == "Your existing entries have been prepared. Nothing has been uploaded yet."
        assert "Already prepared (local only)" in dialog.details.text()
        assert "Entries already carrying sync history: 3" in dialog.details.text()
        assert "Entries to receive bootstrap history: 0" in dialog.details.text()
        assert "Production journal identity: Assigned" in dialog.details.text()
        assert not dialog.prepare_button.isEnabled()
        for forbidden in ("Connected", "Up to date", "Last synced"):
            assert forbidden not in dialog.state_label.text()
            assert forbidden not in dialog.summary_label.text()
            assert forbidden not in dialog.details.text()
            assert forbidden not in dialog.status.text()
        enrollment = read_enrollment(session.connection)
        assert enrollment is not None
        assert str(enrollment[0]) not in dialog.details.text()
        assert any(info.valid and info.kind == "safety" for info in session.backups.list())
        assert session.episodes.list() == expected_episodes

        dialog.reject()
        window.close()
        session.close()

        session = JournalSession(database)
        window = MainWindow(session.episodes, zone=zone, now=lambda: full_episode.occurred_at, backup_session=session)
        window.sync_action.trigger()
        reopened_dialog = window.enrollment_dialog
        assert reopened_dialog.state_label.text() == "Journal ready for sync"
        assert reopened_dialog.summary_label.text() == "Your existing entries have been prepared. Nothing has been uploaded yet."
        assert "Entries to receive bootstrap history: 0" in reopened_dialog.details.text()
        assert not reopened_dialog.prepare_button.isEnabled()
        assert session.inspect_enrollment().journal_id == enrollment[0]
        assert session.episodes.list() == expected_episodes
    finally:
        if window.enrollment_dialog:
            window.enrollment_dialog.reject()
        window.close()
        session.close()


@pytest.mark.parametrize(
    "error_factory,expected_message",
    [
        (lambda: BackupError("private disk details"), "Zymptom could not create a safety backup, so your journal was not changed."),
        (lambda: SyncError("private raw graph error"), "Zymptom found incomplete sync history and did not change your journal."),
        (lambda: RuntimeError("private SQL stack trace"), "Zymptom could not prepare this journal for sync. Your entries were not changed."),
    ],
)
def test_enrollment_ui_surfaces_ordinary_failure_messages_without_leaking_details(
    qapp, zone, tmp_path, full_episode, monkeypatch, error_factory, expected_message
):
    session = JournalSession(tmp_path / "production.sqlite3")
    insert_untracked(session.connection, deepcopy(full_episode))
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    try:
        window.open_sync_dialog()
        dialog = window.enrollment_dialog
        monkeypatch.setattr(session, "enroll_sync", lambda **_kwargs: (_ for _ in ()).throw(error_factory()))
        dialog.confirm_enrollment()
        dialog.confirmation.button(QMessageBox.StandardButton.Yes).click()
        assert dialog.status.text() == expected_message
        assert "private" not in dialog.status.text()
        assert dialog.prepare_button.isEnabled()
        assert read_enrollment(session.connection) is None
        assert read_changes(session.connection) == {}
    finally:
        if window.enrollment_dialog:
            window.enrollment_dialog.reject()
        window.close()
        session.close()


def test_sandbox_mode_does_not_expose_production_enrollment(qapp, tmp_path):
    session = JournalSession(tmp_path / "sandbox.sqlite3")
    sandbox = SandboxWindow(session, tmp_path / "production.sqlite3")
    try:
        assert sandbox.enrollment_dialog is None
        sandbox.open_enrollment_dialog()
        assert sandbox.enrollment_dialog is None
        assert "disabled in the disposable sandbox" in sandbox.statusBar().currentMessage()
        sandbox.sync_action.trigger()
        assert sandbox.enrollment_dialog is None
        assert not hasattr(sandbox.sync_dialog, "prepare_button")
        assert read_enrollment(session.connection) is None
    finally:
        sandbox.close()
        session.close()
