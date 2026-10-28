"""Explicit desktop sandbox launcher and small manual sync controls."""

import sqlite3
import sys
import threading
import hashlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from zymptom.sandbox.auth import AuthError, config_path, read_config, sign_in
from zymptom.sandbox.isolation import IsolationError, sandbox_database
from zymptom.sandbox.sync import sync_disposable
from zymptom.sandbox.transport import FirestoreMailbox, TransportError
from zymptom.persistence.backups import BackupError
from zymptom.services.journal import JournalSession
from zymptom.sync.contract import SyncError
from zymptom.sync.identity import DeviceIdentity
from zymptom.sync.service import SyncService

from .branding import application_icon
from .theme import apply_theme
from .window import MainWindow
from .sync_presentation import SyncState, candidate_text, failure_kind


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
            if not self.cancel.is_set():
                self.failure.emit(failure_kind(exc))
        except Exception:
            self.failure.emit("signin")


class SyncWorker(QThread):
    success = Signal(object, int, bool)
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
                mailbox = FirestoreMailbox(self.config.firebase_project_id, self.auth)
                result, count = sync_disposable(session, mailbox)
                pending = SyncService(session.episodes, before_merge=session.before_destructive_change).pending(f"firebase-sandbox:{mailbox.journal_id}")
            self.success.emit(result, count, not pending.changes)
        except (AuthError, TransportError, IsolationError, BackupError, SyncError, sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            self.failure.emit(failure_kind(exc))


class SandboxWindow(MainWindow):
    def __init__(self, session, production):
        super().__init__(session.episodes, backup_session=session)
        self.session, self.production = session, production
        self.auth = None
        self.config = None
        self.worker = None
        self.sync_state = SyncState()
        self.last_sync = None
        self.backup_action.setEnabled(False)  # Restore must never import a normal journal.
        self.manual_backup_action.setEnabled(False)
        self.recovery_action.setEnabled(False)
        panel = QWidget(self)
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Sandbox Sync · Disposable Data")
        label.setProperty("tone", "secondary")
        self.summary_state = QLabel()
        self.summary_state.setAccessibleName("Sync status")
        open_sync = QPushButton("Sync…")
        open_sync.clicked.connect(self.open_sync)
        layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(self.summary_state)
        layout.addWidget(open_sync)
        self.centralWidget().layout().insertWidget(0, panel)
        self.sync_dialog = QDialog(self)
        self.sync_dialog.setWindowTitle("Sync")
        self.sync_dialog.resize(540, 280)
        sync_layout = QVBoxLayout(self.sync_dialog)
        sync_layout.setContentsMargins(20, 20, 20, 20)
        sync_layout.setSpacing(10)
        heading = QLabel("Sync")
        heading.setStyleSheet("font-size: 18px; font-weight: bold;")
        sync_layout.addWidget(heading)
        notice = QLabel("Sandbox Sync · Disposable Data\nOnly test entries are shared. Your desktop journal is separate.")
        notice.setProperty("tone", "secondary")
        notice.setWordWrap(True)
        sync_layout.addWidget(notice)
        self.sandbox_state = QLabel()
        self.sandbox_state.setWordWrap(True)
        self.sandbox_state.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.sandbox_state.setAccessibleName("Sync status")
        self.account_label = QLabel()
        self.account_label.setWordWrap(True)
        self.account_label.setProperty("tone", "secondary")
        self.last_sync_label = QLabel("Never synced")
        self.last_sync_label.setProperty("tone", "secondary")
        self.sync_message = QLabel()
        self.sync_message.setWordWrap(True)
        self.sync_message.setAccessibleName("Sync message")
        actions = QHBoxLayout()
        self.signin = QPushButton("Connect with Google")
        self.cancel_signin = QPushButton("Cancel sign-in")
        self.signout = QPushButton("Disconnect")
        self.sync_button = QPushButton("Sync now")
        self.sync_button.setProperty("primary", True)
        self.conflict_button = QPushButton("Review conflict")
        for button in (self.signin, self.cancel_signin, self.sync_button, self.signout):
            actions.addWidget(button)
        sync_layout.addWidget(self.sandbox_state)
        sync_layout.addWidget(self.account_label)
        sync_layout.addWidget(self.last_sync_label)
        sync_layout.addWidget(self.sync_message)
        sync_layout.addLayout(actions)
        sync_layout.addWidget(self.conflict_button)
        sync_layout.addStretch(1)
        self.signin.clicked.connect(self.begin_signin)
        self.cancel_signin.clicked.connect(self.cancel_auth)
        self.signout.clicked.connect(self.sign_out)
        self.sync_button.clicked.connect(self.begin_sync)
        self.conflict_button.clicked.connect(self.inspect_conflicts)
        self.setWindowTitle("Zymptom — SANDBOX SYNC · DISPOSABLE DATA")
        self._refresh_conflict_count()
        self._controls()

    def open_sync(self):
        self.sync_dialog.show()
        self.sync_dialog.raise_()
        self.sync_dialog.activateWindow()

    def _settings_key(self):
        identity = f"{self.config.firebase_project_id}:{self.auth.uid}".encode()
        return hashlib.sha256(identity).hexdigest()

    def _settings(self):
        return QSettings(str(config_path().parent / "sync-ui.ini"), QSettings.Format.IniFormat)

    def _show_state(self):
        label = self.sync_state.label
        self.sandbox_state.setText(label)
        self.sandbox_state.setAccessibleName(f"Sync status: {label}")
        self.summary_state.setText(label)
        self.summary_state.setAccessibleName(f"Sync status: {label}")
        self.sync_message.setText(self.sync_state.message)
        self.sync_message.setVisible(bool(self.sync_state.message))
        email = getattr(self.auth, "email", None) if self.auth else None
        self.account_label.setText(f"Google account: {email}" if email else "Connected with Google" if self.auth else "")
        self.account_label.setVisible(bool(self.auth))
        self.last_sync_label.setText(f"Last synced: {self.last_sync.astimezone().strftime('%b %d, %Y · %I:%M %p')}" if self.last_sync else "Never synced")
        self.conflict_button.setVisible(self.sync_state.conflicts > 0)

    def _refresh_conflict_count(self):
        try:
            count = len(SyncService(self.session.episodes, before_merge=self.session.before_destructive_change).unresolved_conflicts())
            self.sync_state.conflicts = count
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            self.sync_state.failure = "other"
        self._show_state()

    def open_backup_dialog(self):
        self.statusBar().showMessage("Restore is disabled in the disposable sandbox.")

    def back_up_journal(self):
        self.statusBar().showMessage("Journal backup is disabled in the disposable sandbox.")

    def open_recovery_dialog(self):
        self.statusBar().showMessage("Journal recovery is disabled in the disposable sandbox.")

    def open_sync_dialog(self):
        self.open_sync()

    def open_enrollment_dialog(self):
        self.statusBar().showMessage("Production sync setup is disabled in the disposable sandbox.")

    def _restore_sync_session(self):
        pass

    def _controls(self):
        busy = self.worker is not None
        signed = self.auth is not None
        self.signin.setEnabled(not busy and not signed)
        self.cancel_signin.setEnabled(isinstance(self.worker, AuthWorker))
        self.cancel_signin.setVisible(isinstance(self.worker, AuthWorker))
        self.signout.setEnabled(not busy and signed)
        self.sync_button.setEnabled(not busy and signed)
        self.conflict_button.setEnabled(not busy)
        for widget in (self.pages, self.log_button, self.today_button, self.history_button, self.export_button):
            widget.setEnabled(not busy)
        self.new_shortcut.setEnabled(not busy)
        self.sync_state.busy = busy
        self.sync_state.connecting = isinstance(self.worker, AuthWorker)
        self.sync_state.connected = signed
        self._show_state()

    def _available(self):
        if self.editor is not None or self.export_dialog is not None or self.backup_dialog is not None:
            self.sync_message.setText("Close the current editor or dialog before syncing.")
            self.sync_message.show()
            return False
        return self.worker is None

    def begin_signin(self):
        if not self._available():
            return
        try:
            self.config = read_config()
        except AuthError:
            self.sync_state.failure = "configuration"
            self._show_state()
            return
        self.sync_state.failure = None
        self.worker = AuthWorker(self.config, self)
        self.worker.success.connect(self._signed_in)
        self.worker.failure.connect(self._worker_failure)
        self._start_worker()

    def _signed_in(self, session):
        self.auth = session
        self.sync_state.failure = None
        self.sync_state.successful = False
        try:
            saved = self._settings().value(self._settings_key(), "")
            self.last_sync = datetime.fromisoformat(saved) if saved else None
        except (ValueError, TypeError):
            self.last_sync = None

    def sign_out(self):
        self.auth = None
        self.config = None
        self.last_sync = None
        self.sync_state.failure = None
        self.sync_state.successful = False
        self._controls()

    def cancel_auth(self):
        if isinstance(self.worker, AuthWorker):
            self.worker.cancel.set()
            self.sync_message.setText("Cancelling sign-in…")
            self.sync_message.show()

    def begin_sync(self):
        if not self.auth or not self._available():
            return
        self.sync_state.failure = None
        self.worker = SyncWorker(self.session.database, self.production, self.session._device_id, self.config, self.auth, self)
        self.worker.success.connect(self._sync_finished)
        self.worker.failure.connect(self._worker_failure)
        self._start_worker()

    def _worker_failure(self, kind):
        self.sync_state.failure = kind if kind in {"offline", "auth", "permission", "integrity", "backup", "signin", "configuration", "cancelled", "other"} else "other"
        self.sync_state.successful = False
        if kind == "auth":
            self.auth = None
        self._show_state()

    def _start_worker(self):
        self.worker.finished.connect(self._worker_finished)
        self._controls()
        self.worker.start()

    def _worker_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self._controls()

    def _sync_finished(self, result, count, up_to_date=True):
        self.refresh()
        self.history.refresh()
        self._refresh_conflict_count()
        self.sync_state.successful = up_to_date
        self.sync_state.failure = None
        self.last_sync = datetime.now(timezone.utc)
        if self.auth and self.config:
            settings = self._settings()
            settings.setValue(self._settings_key(), self.last_sync.isoformat())
            settings.sync()
        self._show_state()

    def _open_editor(self, episode=None):
        previous = self.editor
        super()._open_editor(episode)
        if self.editor is not None and self.editor is not previous:
            self.editor.accepted.connect(self._local_change)
            self.editor.deleted.connect(self._local_change)

    def _local_change(self):
        self.sync_state.successful = False
        self._show_state()

    def inspect_conflicts(self):
        if not self._available():
            return
        service = SyncService(self.session.episodes, before_merge=self.session.before_destructive_change)
        conflicts = service.unresolved_conflicts()
        if not conflicts:
            self._refresh_conflict_count()
            return
        conflict = conflicts[0]
        dialog = QDialog(self)
        dialog.setWindowTitle("Review conflict")
        layout = QVBoxLayout(dialog)
        deleted = [candidate.deleted for candidate in conflict.candidates]
        explanation = QLabel("This entry was deleted on all devices. Choose a version to confirm the deletion." if all(deleted) else
                             "This entry was deleted on one device but changed on another." if any(deleted) else
                             "This entry changed differently on two devices. Choose the version you want to keep.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        versions = QHBoxLayout()
        def resolve(candidate):
            try:
                service.resolve(conflict.record_id, chosen_change_id=candidate.change_id)
                dialog.accept()
                self.refresh()
                self.history.refresh()
                self._refresh_conflict_count()
                self.sync_state.successful = False
                self._show_state()
                self.sync_message.setText("Version saved. Sync now to share it.")
                self.sync_message.show()
            except (SyncError, sqlite3.Error, OSError, ValueError, RuntimeError):
                QMessageBox.warning(dialog, "Resolution failed", "The conflict was not resolved. Inspect it again and retry.")
        for index, candidate in enumerate(conflict.candidates):
            column = QVBoxLayout()
            column.addWidget(QLabel(f"Version {index + 1}"))
            view = QTextEdit()
            view.setReadOnly(True)
            view.setAccessibleName(f"Version {index + 1} details")
            view.setPlainText(candidate_text(candidate, self.zone))
            column.addWidget(view)
            choose = QPushButton("Keep this version" if index == 0 else "Keep the other version" if index == 1 else f"Keep version {index + 1}")
            choose.clicked.connect(lambda _checked=False, selected=candidate: resolve(selected))
            column.addWidget(choose)
            versions.addLayout(column)
        layout.addLayout(versions)
        dialog.resize(800, 550)
        dialog.finished.connect(dialog.deleteLater)
        dialog.exec()

    def closeEvent(self, event):
        if self.worker is not None:
            if isinstance(self.worker, AuthWorker):
                self.worker.cancel.set()
            self.sync_message.setText("Finishing the current sync operation; close again when it completes.")
            self.sync_message.show()
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
