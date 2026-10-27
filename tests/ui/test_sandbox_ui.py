from PySide6.QtCore import QTimer
import time
import os
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton, QTextEdit

from zymptom.sandbox.auth import AuthError, DesktopConfig, FirebaseSession
from zymptom.domain import EpisodeOutcome
from zymptom.services.journal import JournalSession
from zymptom.sync.contract import SyncBatch
from zymptom.sync.simulator import LocalSimulator
from zymptom.ui.sandbox_app import SandboxWindow


def test_normal_window_has_no_sandbox_controls(window):
    assert not hasattr(window, "sync_button")


def test_configuration_failure_is_clear_and_does_not_leak_details(qapp, tmp_path, monkeypatch):
    from zymptom.ui import sandbox_app
    session = JournalSession(tmp_path / "disposable.sqlite3")
    window = SandboxWindow(session, tmp_path / "production.sqlite3")
    monkeypatch.setattr(sandbox_app, "read_config", lambda: (_ for _ in ()).throw(AuthError("private details")))
    try:
        window.begin_signin()
        assert window.sandbox_state.text() == "Needs attention"
        assert "setup is incomplete" in window.sync_message.text()
        assert "private details" not in window.sync_message.text()
        assert window.signin.isEnabled()
    finally:
        window.close()
        session.close()


def test_sandbox_is_labeled_and_restore_is_disabled(qapp, tmp_path):
    session = JournalSession(tmp_path / "disposable.sqlite3")
    window = SandboxWindow(session, tmp_path / "production.sqlite3")
    try:
        assert "SANDBOX" in window.windowTitle()
        assert window.sync_button.text() == "Sync now"
        assert window.sandbox_state.text() == "Not connected"
        assert window.signin.isEnabled() and not window.signout.isEnabled()
        assert window.conflict_button.isHidden()
        assert window.last_sync_label.text() == "Never synced"
        assert not window.sync_button.isEnabled()
        assert not window.backup_action.isEnabled()
        window.open_backup_dialog()
        assert window.backup_dialog is None
        window.config = DesktopConfig("key", "demo-project", "client")
        window._signed_in(FirebaseSession("uid", "secret-token", time.monotonic() + 100, "test@example.test"))
        window._controls()
        assert window.sandbox_state.text() == "Connected"
        assert window.sync_button.isEnabled() and window.signout.isEnabled() and not window.signin.isEnabled()
        assert window.account_label.text() == "Google account: test@example.test"
        window._sync_finished(type("Result", (), {"applied": ()})(), 0)
        assert window.sandbox_state.text() == "Up to date"
        assert window.last_sync_label.text().startswith("Last synced:")
        saved = window.last_sync_label.text()
        window.sign_out()
        assert window.last_sync_label.text() == "Never synced"
        window.config = DesktopConfig("key", "demo-project", "client")
        window._signed_in(FirebaseSession("uid", "secret-token", time.monotonic() + 100, "test@example.test"))
        window._controls()
        assert window.last_sync_label.text() == saved
        window.sign_out()
        window.config = DesktopConfig("key", "demo-project", "client")
        window._signed_in(FirebaseSession("other-uid", "secret-token", time.monotonic() + 100))
        window._controls()
        assert window.last_sync_label.text() == "Never synced"
        window._sync_finished(type("Result", (), {"applied": ()})(), 0, False)
        assert window.sandbox_state.text() == "Connected"
        assert window.last_sync_label.text().startswith("Last synced:")
        window._sync_finished(type("Result", (), {"applied": ()})(), 0)
        window._local_change()
        assert window.sandbox_state.text() == "Connected"
        window._worker_failure("offline")
        assert window.sandbox_state.text() == "Offline"
        assert "entries are safe" in window.sync_message.text()
        window._worker_failure("private-exception-text")
        assert "private-exception-text" not in window.sync_message.text()
        window._worker_failure("auth")
        window._controls()
        assert window.sandbox_state.text() == "Not connected"
        assert window.signin.isEnabled() and not window.sync_button.isEnabled()
        assert "connection expired" in window.sync_message.text()
    finally:
        window.close()
        session.close()


@pytest.mark.parametrize("delete_left", [False, True])
def test_conflict_inspection_and_explicit_resolution(qapp, tmp_path, full_episode, delete_left):
    simulator = LocalSimulator(tmp_path / "isolated")
    a, b = simulator.desktop, simulator.phone
    a.episodes.repository.create(full_episode)
    simulator.exchange()
    for device, notes in ((a, "Left candidate"), (b, "Right candidate")):
        episode = device.episodes.get(full_episode.id)
        if delete_left and device is a:
            device.episodes.delete(episode.id)
        else:
            episode.notes = notes
            device.episodes.update(episode)
    simulator.exchange()
    window = SandboxWindow(a.session, tmp_path / "production.sqlite3")
    observed = []
    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        observed.extend(view.toPlainText() for view in dialog.findChildren(QTextEdit))
        observed.append(dialog.windowTitle())
        observed.append(" ".join(label.text() for label in dialog.findChildren(QLabel)))
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "Keep this version").click()
    try:
        assert window.sandbox_state.text() == "Needs attention"
        assert not window.conflict_button.isHidden()
        QTimer.singleShot(0, choose)
        window.inspect_conflicts()
        assert len(observed) == 4 and observed[2] == "Review conflict"
        assert any("candidate" in value for value in observed[:2])
        if delete_left:
            assert any("deleted the entry" in value for value in observed[:2])
            assert "deleted on one device but changed" in observed[3]
        assert all(str(full_episode.id) not in value and '"record_id"' not in value for value in observed)
        assert not a.sync.unresolved_conflicts()
        assert window.conflict_button.isHidden()
        assert "Version saved" in window.sync_message.text()
        assert any(change.operation == "resolve" for change in a.sync.pending("test").changes)
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


def test_sync_worker_does_not_report_up_to_date_when_new_work_arrives(qapp, tmp_path, monkeypatch):
    from zymptom.ui import sandbox_app
    session = JournalSession(tmp_path / "disposable.sqlite3")
    session.episodes.record(activity="First", outcome=EpisodeOutcome.STOPPED_EARLY)
    class Mailbox:
        journal_id = "00000000-0000-0000-0000-000000000123"
        def __init__(self):
            self.changes = []
        def connect(self):
            return self.journal_id
        def upload(self, change):
            self.changes.append(change)
        def pull(self):
            session.episodes.record(activity="Added during sync", outcome=EpisodeOutcome.STOPPED_EARLY)
            return SyncBatch(tuple(self.changes))
    monkeypatch.setattr(sandbox_app, "sandbox_database", lambda *_args: session.database)
    monkeypatch.setattr(sandbox_app, "FirestoreMailbox", lambda *_args: Mailbox())
    worker = sandbox_app.SyncWorker(session.database, tmp_path / "production.sqlite3", session._device_id,
                                    DesktopConfig("key", "demo-project", "client"),
                                    FirebaseSession("uid", "token", time.monotonic() + 100))
    results, failures = [], []
    worker.success.connect(lambda result, count, current: results.append((result, count, current)))
    worker.failure.connect(failures.append)
    try:
        worker.run()
        assert not failures and len(results) == 1
        assert results[0][2] is False
        assert len(session.episodes.list()) == 2
    finally:
        session.close()


def test_conflict_dialog_capture_and_teardown_exit_cleanly(tmp_path):
    script = r'''
import gc, sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from zymptom.domain import EpisodeOutcome
from zymptom.sync.simulator import LocalSimulator
from zymptom.ui.sandbox_app import SandboxWindow
from zymptom.ui.theme import apply_theme
root = Path(sys.argv[1])
app = QApplication([]); app.setQuitOnLastWindowClosed(False); apply_theme(app)
sim = LocalSimulator(root / 'sim')
episode = sim.desktop.episodes.record(activity='DEMO: conflict', outcome=EpisodeOutcome.STOPPED_EARLY)
sim.exchange()
for device, note in ((sim.desktop, 'Left'), (sim.phone, 'Right')):
    draft = device.episodes.get(episode.id); draft.notes = note; device.episodes.update(draft)
sim.exchange()
window = SandboxWindow(sim.desktop.session, root / 'normal.sqlite3'); window.show(); app.processEvents()
def capture():
    dialog = QApplication.activeModalWidget(); dialog.grab().save(str(root / 'conflict.png')); dialog.reject()
QTimer.singleShot(0, capture); window.inspect_conflicts(); window.close(); app.processEvents()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
del window; gc.collect(); sim.close(); del sim; app.processEvents()
'''
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "XDG_CONFIG_HOME": str(tmp_path / "config")}
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", script, str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
