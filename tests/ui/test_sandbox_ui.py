from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QMessageBox, QPushButton, QTextEdit

from zymptom.services.journal import JournalSession
from zymptom.sync.simulator import LocalSimulator
from zymptom.ui.sandbox_app import SandboxWindow


def test_normal_window_has_no_sandbox_controls(window):
    assert not hasattr(window, "sync_button")


def test_sandbox_is_labeled_and_restore_is_disabled(qapp, tmp_path):
    session = JournalSession(tmp_path / "disposable.sqlite3")
    window = SandboxWindow(session, tmp_path / "production.sqlite3")
    try:
        assert "SANDBOX" in window.windowTitle()
        assert window.sync_button.text() == "Sync disposable entries"
        assert window.conflict_count.text() == "Unresolved conflicts: 0"
        assert not window.sync_button.isEnabled()
        assert not window.backup_action.isEnabled()
        window.open_backup_dialog()
        assert window.backup_dialog is None
        window._sync_finished(type("Result", (), {"applied": ()})(), 2)
        assert "unresolved conflicts: 2" in window.sandbox_state.text()
    finally:
        window.close()
        session.close()


def test_conflict_inspection_and_explicit_resolution(qapp, tmp_path, full_episode):
    simulator = LocalSimulator(tmp_path / "isolated")
    a, b = simulator.desktop, simulator.phone
    a.episodes.repository.create(full_episode)
    simulator.exchange()
    for device, notes in ((a, "Left candidate"), (b, "Right candidate")):
        episode = device.episodes.get(full_episode.id)
        episode.notes = notes
        device.episodes.update(episode)
    simulator.exchange()
    window = SandboxWindow(a.session, tmp_path / "production.sqlite3")
    observed = []
    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        selector = dialog.findChild(QComboBox)
        assert selector.currentIndex() == -1
        selector.setCurrentIndex(0)
        observed.append(dialog.findChild(QTextEdit).toPlainText())
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "Resolve using selected version").click()
    try:
        assert window.conflict_count.text() == "Unresolved conflicts: 1"
        QTimer.singleShot(0, choose)
        window.inspect_conflicts()
        assert observed and "candidate" in observed[0]
        assert not a.sync.unresolved_conflicts()
        assert window.conflict_count.text() == "Unresolved conflicts: 0"
        assert "resolved locally" in window.sandbox_state.text()
    finally:
        window.close()
        simulator.close()


def test_sandbox_initialization_failure_never_opens_journal(qapp, monkeypatch):
    from zymptom.ui import sandbox_app
    opened = []
    monkeypatch.setattr(sandbox_app, "sandbox_database", lambda *_args: (_ for _ in ()).throw(sandbox_app.IsolationError("no")))
    monkeypatch.setattr(sandbox_app, "JournalSession", lambda *_args, **_kwargs: opened.append(True))
    messages = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    assert sandbox_app.run_sandbox() == 1
    assert not opened and messages


def test_explicit_sandbox_launch_opens_only_the_dedicated_database(qapp, tmp_path, monkeypatch):
    from zymptom.ui import sandbox_app
    data_root = tmp_path / "data"
    production = tmp_path / "normal"
    production.mkdir()
    sentinel = production / "zymptom.sqlite3"
    sentinel.write_bytes(b"normal journal sentinel")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_root))
    monkeypatch.setattr(sandbox_app.QStandardPaths, "writableLocation", lambda *_args: str(production))
    monkeypatch.setattr(QApplication, "exec", lambda *_args: 0)
    assert sandbox_app.run_sandbox() == 0
    assert sentinel.read_bytes() == b"normal journal sentinel"
    assert (data_root / "zymptom/sandbox-sync/journal.sqlite3").is_file()
