"""Explicit desktop sandbox launcher and small manual sync controls."""

import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from zymptom.sandbox.auth import AuthError, config_path, read_config, sign_in
from zymptom.sandbox.isolation import IsolationError, sandbox_database
from zymptom.sandbox.sync import sync_disposable
from zymptom.sandbox.transport import FirestoreMailbox, TransportError
from zymptom.persistence.backups import BackupError
from zymptom.services.journal import JournalSession
from zymptom.sync.contract import SyncError, episode_data
from zymptom.sync.identity import DeviceIdentity
from zymptom.sync.service import SyncService

from .branding import application_icon
from .theme import apply_theme
from .window import MainWindow


class AuthWorker(QThread):
    success = Signal(object)
    failure = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.cancel = threading.Event()

    def run(self):
        try:
            result = sign_in(self.config, cancel=self.cancel)
            if not self.cancel.is_set():
                self.success.emit(result)
        except AuthError as exc:
            self.failure.emit(str(exc))
        except Exception:
            self.failure.emit("Sign-in failed. Local entries remain available.")


class SyncWorker(QThread):
    success = Signal(object, int)
    failure = Signal(str)

    def __init__(self, path, production, device, config, auth, parent=None):
        super().__init__(parent)
        self.path, self.production, self.device, self.config, self.auth = path, production, device, config, auth

    def run(self):
        try:
            checked = sandbox_database(self.production)
            if checked != self.path:
                raise IsolationError("Sandbox database location changed.")
            with closing(JournalSession(checked, device_id=self.device)) as session:
                result, count = sync_disposable(session, FirestoreMailbox(self.config.firebase_project_id, self.auth))
            self.success.emit(result, count)
        except BackupError:
            self.failure.emit("The verified safety backup failed. No incoming changes were applied; retry when backup storage is available.")
        except (AuthError, TransportError, IsolationError) as exc:
            self.failure.emit(str(exc))
        except (SyncError, sqlite3.Error, OSError, ValueError, RuntimeError):
            self.failure.emit("Sandbox sync failed. The local journal remains available; check the connection and retry.")


class SandboxWindow(MainWindow):
    def __init__(self, session, production):
        super().__init__(session.episodes, backup_session=session)
        self.session, self.production = session, production
        self.auth = None
        self.config = None
        self.worker = None
        self.backup_action.setEnabled(False)  # Restore must never import a normal journal.
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("SANDBOX SYNC · DISPOSABLE DATA")
        label.setWordWrap(True)
        self.sandbox_state = QLabel("Signed out. Only disposable entries belong here.")
        self.sandbox_state.setWordWrap(True)
        self.conflict_count = QLabel()
        actions = QHBoxLayout()
        self.signin = QPushButton("Google Sign in")
        self.cancel_signin = QPushButton("Cancel sign-in")
        self.signout = QPushButton("Sign out")
        self.sync_button = QPushButton("Sync disposable entries")
        self.conflict_button = QPushButton("Inspect conflicts")
        for button in (self.signin, self.cancel_signin, self.signout, self.sync_button, self.conflict_button):
            actions.addWidget(button)
        layout.addWidget(label)
        layout.addWidget(self.sandbox_state)
        layout.addWidget(self.conflict_count)
        layout.addLayout(actions)
        self.centralWidget().layout().insertWidget(0, panel)
        self.signin.clicked.connect(self.begin_signin)
        self.cancel_signin.clicked.connect(self.cancel_auth)
        self.signout.clicked.connect(self.sign_out)
        self.sync_button.clicked.connect(self.begin_sync)
        self.conflict_button.clicked.connect(self.inspect_conflicts)
        self.setWindowTitle("Zymptom — SANDBOX SYNC · DISPOSABLE DATA")
        self._refresh_conflict_count()
        self._controls()

    def _refresh_conflict_count(self):
        try:
            count = len(SyncService(self.session.episodes, before_merge=self.session.before_destructive_change).unresolved_conflicts())
            self.conflict_count.setText(f"Unresolved conflicts: {count}")
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            self.conflict_count.setText("Could not load sandbox conflicts.")

    def open_backup_dialog(self):
        self.statusBar().showMessage("Restore is disabled in the disposable sandbox.")

    def _controls(self):
        busy = self.worker is not None
        signed = self.auth is not None
        self.signin.setEnabled(not busy and not signed)
        self.cancel_signin.setEnabled(isinstance(self.worker, AuthWorker))
        self.signout.setEnabled(not busy and signed)
        self.sync_button.setEnabled(not busy and signed)
        self.conflict_button.setEnabled(not busy)
        for widget in (self.pages, self.log_button, self.today_button, self.history_button, self.export_button):
            widget.setEnabled(not busy)
        self.new_shortcut.setEnabled(not busy)

    def _available(self):
        if self.editor is not None or self.export_dialog is not None or self.backup_dialog is not None:
            self.sandbox_state.setText("Close the current editor or dialog before sandbox sync.")
            return False
        return self.worker is None

    def begin_signin(self):
        if not self._available():
            return
        try:
            self.config = read_config()
        except AuthError as exc:
            self.sandbox_state.setText(str(exc))
            return
        self.sandbox_state.setText("Complete Google sign-in in your browser. This times out if cancelled.")
        self.worker = AuthWorker(self.config, self)
        self.worker.success.connect(self._signed_in)
        self.worker.failure.connect(self.sandbox_state.setText)
        self._start_worker()

    def _signed_in(self, session):
        self.auth = session
        self.sandbox_state.setText("Signed in to the Firebase sandbox. Tokens are kept in memory only.")

    def sign_out(self):
        self.auth = None
        self.config = None
        self.sandbox_state.setText("Signed out. Disposable entries remain on this computer.")
        self._controls()

    def cancel_auth(self):
        if isinstance(self.worker, AuthWorker):
            self.worker.cancel.set()
            self.sandbox_state.setText("Cancelling Google sign-in…")

    def begin_sync(self):
        if not self.auth or not self._available():
            return
        self.sandbox_state.setText("Syncing disposable entries…")
        self.worker = SyncWorker(self.session.database, self.production, self.session._device_id, self.config, self.auth, self)
        self.worker.success.connect(self._sync_finished)
        self.worker.failure.connect(self.sandbox_state.setText)
        self._start_worker()

    def _start_worker(self):
        self.worker.finished.connect(self._worker_finished)
        self._controls()
        self.worker.start()

    def _worker_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self._controls()

    def _sync_finished(self, result, count):
        self.refresh()
        self.history.refresh()
        self._refresh_conflict_count()
        self.sandbox_state.setText(f"Sync complete. Applied {len(result.applied)} changes; unresolved conflicts: {count}.")

    def inspect_conflicts(self):
        if not self._available():
            return
        service = SyncService(self.session.episodes, before_merge=self.session.before_destructive_change)
        conflicts = service.unresolved_conflicts()
        if not conflicts:
            self.sandbox_state.setText("No unresolved sandbox conflicts.")
            return
        conflict = conflicts[0]
        dialog = QDialog(self)
        dialog.setWindowTitle("Sandbox conflict · developer proof")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Record {conflict.record_id} · base {conflict.base.change_id if conflict.base else 'none'}"))
        selector = QComboBox()
        view = QTextEdit()
        view.setReadOnly(True)
        for candidate in conflict.candidates:
            description = candidate.episode.activity if candidate.episode else "Deleted entry"
            selector.addItem(f"{description} · {candidate.operation} · rev {candidate.revision} · {candidate.change_id}", candidate)
        selector.setCurrentIndex(-1)
        def show_candidate(index):
            candidate = selector.itemData(index)
            if candidate is None:
                view.clear()
                return
            import json
            data = episode_data(candidate.episode) if candidate.episode else {"deleted": True}
            view.setPlainText(f"Device: {candidate.device_id}\nChange: {candidate.change_id}\n\n" + json.dumps(data, ensure_ascii=False, indent=2))
        selector.currentIndexChanged.connect(show_candidate)
        layout.addWidget(selector)
        layout.addWidget(view)
        choose = QPushButton("Resolve using selected version")
        def resolve():
            candidate = selector.currentData()
            if candidate is None:
                return
            try:
                service.resolve(conflict.record_id, chosen_change_id=candidate.change_id)
                dialog.accept()
                self.refresh()
                self.history.refresh()
                self._refresh_conflict_count()
                self.sandbox_state.setText("Conflict resolved locally. Sync to share the resolution.")
            except (SyncError, sqlite3.Error, OSError, ValueError, RuntimeError):
                QMessageBox.warning(dialog, "Resolution failed", "The conflict was not resolved. Inspect it again and retry.")
        choose.clicked.connect(resolve)
        layout.addWidget(choose)
        dialog.resize(600, 500)
        dialog.exec()

    def closeEvent(self, event):
        if self.worker is not None:
            if isinstance(self.worker, AuthWorker):
                self.worker.cancel.set()
            self.sandbox_state.setText("Finishing the current sandbox operation; close again when it completes.")
            event.ignore()
            return
        self.auth = None
        super().closeEvent(event)


def run_sandbox():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("Zymptom")
    app.setApplicationName("Zymptom")
    apply_theme(app)
    app.setWindowIcon(application_icon())
    production = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)) / "zymptom.sqlite3"
    try:
        database = sandbox_database(production)
        device = DeviceIdentity(config_path().parent / "device.json").get()
        session = JournalSession(database, device_id=device)
    except (IsolationError, OSError, ValueError, sqlite3.Error, RuntimeError):
        QMessageBox.critical(None, "Sandbox unavailable", "The disposable sandbox could not be isolated or opened. The normal journal was not opened.")
        return 1
    with closing(session):
        window = SandboxWindow(session, production)
        window.show()
        return app.exec()
