from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from zymptom.persistence.backups import content_hash
from zymptom.services.journal import JournalSession, ManualBackupError
from zymptom.ui.sandbox_app import SandboxWindow
from zymptom.ui.window import MainWindow


def test_settings_action_default_cancel_success_and_overwrite(qapp, zone, tmp_path, monkeypatch):
    session = JournalSession(tmp_path / "journal.sqlite3")
    window = MainWindow(session.episodes, zone=zone, now=lambda: datetime(2026, 10, 21, 1, tzinfo=UTC), backup_session=session)
    calls, messages = [], []
    selected = [""]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (calls.append((args, kwargs)), (selected[0], ""))[1])
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    answers = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answers[0])
    try:
        assert window.manual_backup_action in window.data_menu.actions()
        assert window.manual_backup_action.isEnabled()
        before = content_hash(session.connection)
        window.manual_backup_action.trigger()
        assert Path(calls[0][0][2]).name == "Zymptom-Journal-2026-10-20.sqlite3"
        assert Path(calls[0][0][2]).parent != session.backups.directory
        assert content_hash(session.connection) == before and messages == []
        selected[0] = str(tmp_path / "user-backup.sqlite3")
        window.manual_backup_action.trigger()
        assert "created successfully" in messages[-1]
        assert session.inspect_recovery(selected[0]).entry_count == 0
        Path(selected[0]).write_bytes(b"do not replace")
        window.manual_backup_action.trigger()
        assert Path(selected[0]).read_bytes() == b"do not replace"
        answers[0] = QMessageBox.StandardButton.Yes
        window.manual_backup_action.trigger()
        assert session.inspect_recovery(selected[0]).entry_count == 0
    finally:
        window.close()
        session.close()


@pytest.mark.parametrize("kind,expected", [("current", "different location"), ("verify", "could not be verified"), ("write", "could not create")])
def test_failures_are_human_readable(qapp, zone, tmp_path, monkeypatch, kind, expected):
    session = JournalSession(tmp_path / "journal.sqlite3")
    window = MainWindow(session.episodes, zone=zone, backup_session=session)
    messages = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(tmp_path / "backup.sqlite3"), ""))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[2]))
    def fail(*args, **kwargs):
        raise ManualBackupError("private details", kind=kind)
    monkeypatch.setattr(session, "create_manual_backup", fail)
    try:
        window.manual_backup_action.trigger()
        assert expected in messages[0] and "private details" not in messages[0]
    finally:
        window.close()
        session.close()


def test_sandbox_disables_action_and_direct_route(qapp, tmp_path, monkeypatch):
    session = JournalSession(tmp_path / "sandbox.sqlite3")
    window = SandboxWindow(session, tmp_path / "production.sqlite3")
    calls = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: calls.append(args))
    try:
        assert not window.manual_backup_action.isEnabled()
        window.manual_backup_action.trigger()
        window.back_up_journal()
        assert calls == [] and "disabled" in window.statusBar().currentMessage()
    finally:
        window.close()
        session.close()
