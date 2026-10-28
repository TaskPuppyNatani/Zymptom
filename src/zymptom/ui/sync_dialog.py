"""Inspect local Z5 readiness, enroll the production journal, and manually sync with Firebase."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import sqlite3
import threading

from PySide6.QtCore import QSettings, QThread, QTimeZone, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTextEdit, QVBoxLayout,
)

from zymptom.dictation.config import configuration_path
from zymptom.persistence.backups import BackupError
from zymptom.sandbox.auth import AuthError, read_config, sign_in
from zymptom.sandbox.isolation import IsolationError
from zymptom.sandbox.transport import FirestoreMailbox, TransportError
from zymptom.sync.contract import SyncError
from zymptom.sync.service import SyncService

from .branding import brand_mark
from .sync_presentation import (
    MESSAGES,
    SyncState,
    candidate_text,
    enrollment_failure_message,
    enrollment_preview_text,
    failure_kind,
    first_sync_preview_text,
    production_sync_details_text,
)
from .time import to_local_datetime
from .widgets import comfortable


class ProductionAuthWorker(QThread):
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
            if not self.cancel.is_set():
                self.failure.emit("signin")


class SyncDialog(QDialog):
    enrolled = Signal()
    synced = Signal()

    def __init__(self, session, *, zone: QTimeZone | None = None, now=None, mailbox_factory=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.zone = zone or QTimeZone.systemTimeZone()
        self.now = now or (lambda: datetime.now(UTC))
        self.mailbox_factory = mailbox_factory
        self.preview = None
        self.sync_preview = None
        self.confirmation = None
        self.worker = None
        self.auth = getattr(parent, "_sync_auth", None) if parent is not None else None
        self.config = getattr(parent, "_sync_config", None) if parent is not None else None
        self.last_sync = getattr(parent, "_sync_last_sync", None) if parent is not None else None
        self.disconnected = getattr(parent, "_sync_disconnected", False) if parent is not None else False
        self.sync_state = SyncState(
            connected=self.auth is not None,
            successful=getattr(parent, "_sync_successful", False) if parent is not None else False,
        )
        self.first_sync_required = True
        self.setWindowTitle("Sync")
        self.setAccessibleName("Production journal sync setup")
        self.setModal(True)
        self.resize(580, 440)
        layout = QVBoxLayout(self)
        heading = QLabel("Sync")
        heading_font = heading.font()
        heading_font.setBold(True)
        heading.setFont(heading_font)
        layout.addWidget(heading)
        self.state_label = QLabel()
        state_font = self.state_label.font()
        state_font.setBold(True)
        self.state_label.setFont(state_font)
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        self.state_label.setWordWrap(True)
        self.state_label.setAccessibleName("Sync status")
        layout.addWidget(self.state_label)
        self.account_label = QLabel()
        self.account_label.setProperty("tone", "secondary")
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_label.setWordWrap(True)
        self.account_label.hide()
        layout.addWidget(self.account_label)
        self.last_sync_label = QLabel("Never synced")
        self.last_sync_label.setProperty("tone", "secondary")
        self.last_sync_label.setTextFormat(Qt.TextFormat.PlainText)
        self.last_sync_label.setWordWrap(True)
        self.last_sync_label.hide()
        layout.addWidget(self.last_sync_label)
        self.summary_label = QLabel()
        self.summary_label.setProperty("tone", "secondary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setAccessibleName("Sync summary")
        layout.addWidget(self.summary_label)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setAccessibleName("Sync enrollment preview")
        layout.addWidget(self.details)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Sync setup status")
        self.status.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sync_message = self.status
        layout.addWidget(self.status)
        self.conflict_button = QPushButton("Review conflict")
        comfortable(self.conflict_button, "Review conflict")
        self.conflict_button.hide()
        layout.addWidget(self.conflict_button)
        layout.addStretch(1)
        actions = QHBoxLayout()
        self.close_button = QPushButton("Close")
        self.cancel_button = self.close_button
        self.prepare_button = QPushButton("Prepare journal for sync…")
        self.signin = QPushButton("Connect with Google")
        self.connect_button = self.signin
        self.cancel_signin = QPushButton("Cancel sign-in")
        self.start_sync_button = QPushButton("Start sync")
        self.sync_button = QPushButton("Sync now")
        self.signout = QPushButton("Disconnect")
        self.disconnect_button = self.signout
        comfortable(self.close_button, "Close sync setup")
        comfortable(self.prepare_button, "Prepare journal for sync")
        comfortable(self.signin, "Connect with Google")
        comfortable(self.cancel_signin, "Cancel sign-in")
        comfortable(self.start_sync_button, "Start first sync")
        comfortable(self.sync_button, "Sync now")
        comfortable(self.signout, "Disconnect sync")
        self.prepare_button.setProperty("primary", True)
        self.start_sync_button.setProperty("primary", True)
        self.sync_button.setProperty("primary", True)
        self.close_button.clicked.connect(self.reject)
        self.prepare_button.clicked.connect(self.confirm_enrollment)
        self.signin.clicked.connect(self.begin_signin)
        self.cancel_signin.clicked.connect(self.cancel_auth)
        self.start_sync_button.clicked.connect(self.start_first_sync)
        self.sync_button.clicked.connect(self.begin_sync)
        self.signout.clicked.connect(self.sign_out)
        self.conflict_button.clicked.connect(self.inspect_conflicts)
        for button in (
            self.close_button,
            self.prepare_button,
            self.signin,
            self.cancel_signin,
            self.start_sync_button,
            self.sync_button,
            self.signout,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addWidget(brand_mark(self))
        self.refresh()
        if self.prepare_button.isEnabled():
            self.prepare_button.setFocus()
        elif self.signin.isEnabled():
            self.signin.setFocus()
        elif self.start_sync_button.isEnabled():
            self.start_sync_button.setFocus()
        elif self.sync_button.isEnabled():
            self.sync_button.setFocus()
        else:
            self.close_button.setFocus()

    def _sync_parent_state(self):
        parent = self.parent()
        if parent is not None:
            parent._sync_auth = self.auth
            parent._sync_config = self.config
            parent._sync_last_sync = self.last_sync
            parent._sync_successful = self.sync_state.successful
            parent._sync_disconnected = self.disconnected

    def _settings(self):
        return QSettings(str(configuration_path().parent / "production-sync.ini"), QSettings.Format.IniFormat)

    def _settings_key(self, journal_id):
        project_id = self.config.firebase_project_id if self.config is not None else "default"
        uid = self.auth.uid if self.auth is not None else "anonymous"
        identity = f"{project_id}:{uid}:{journal_id}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def _load_saved_last_sync(self, journal_id):
        if self.auth is None or self.config is None or journal_id is None:
            return None
        try:
            saved = self._settings().value(self._settings_key(journal_id), "")
            return datetime.fromisoformat(saved) if saved else None
        except (ValueError, TypeError):
            return None

    def _save_last_sync(self, journal_id, timestamp: datetime):
        if self.auth is not None and self.config is not None and journal_id is not None:
            settings = self._settings()
            settings.setValue(self._settings_key(journal_id), timestamp.isoformat())
            settings.sync()

    def refresh(self, *, clear_status: bool = True):
        self.preview = None
        self.sync_preview = None
        if clear_status and not self.sync_state.failure and not self.sync_state.conflicts:
            self.status.clear()
        try:
            preview = self.session.inspect_enrollment()
            self.preview = preview
            if preview.enrolled and preview.bootstrap_required_count == 0:
                if self.last_sync is None and self.auth is not None:
                    self.last_sync = self._load_saved_last_sync(preview.journal_id)
                sync_preview = self.session.inspect_production_sync(has_synced_before=(self.last_sync is not None))
                self.sync_preview = sync_preview
                self.first_sync_required = sync_preview.first_sync_required
                self.sync_state.conflicts = sync_preview.conflict_count
                if sync_preview.pending_change_count > 0 or sync_preview.conflict_count > 0:
                    self.sync_state.successful = False
            else:
                self.first_sync_required = True
                self.sync_state.conflicts = 0
                self.sync_state.successful = False
            self._controls()
        except (BackupError, SyncError, sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            self.state_label.setText("Needs attention")
            self.summary_label.setText("Your local entries remain available on this computer.")
            self.details.setText("Sync readiness could not be verified.")
            self.prepare_button.setEnabled(False)
            self.signin.setEnabled(False)
            self.start_sync_button.setEnabled(False)
            self.sync_button.setEnabled(False)
            self.signout.setEnabled(False)
            self._error(exc)

    def _controls(self):
        busy = self.worker is not None or self.sync_state.busy
        connecting = isinstance(self.worker, ProductionAuthWorker) or self.sync_state.connecting
        signed = self.auth is not None
        enrolled = (
            self.preview is not None
            and self.preview.enrolled
            and self.preview.bootstrap_required_count == 0
        )
        can_prepare = (
            self.preview is not None
            and (not self.preview.enrolled or self.preview.bootstrap_required_count > 0)
            and self.confirmation is None
            and not busy
            and self.session.connection is not None
        )
        self.prepare_button.setEnabled(can_prepare)
        self.prepare_button.setVisible(not enrolled or not signed)
        self.signin.setEnabled(enrolled and not busy and not signed and self.session.connection is not None)
        self.signin.setVisible(enrolled and not signed)
        self.cancel_signin.setEnabled(connecting)
        self.cancel_signin.setVisible(connecting)
        self.start_sync_button.setEnabled(enrolled and not busy and signed and self.first_sync_required)
        self.start_sync_button.setVisible(enrolled and signed and self.first_sync_required)
        self.sync_button.setEnabled(enrolled and not busy and signed and not self.first_sync_required)
        self.sync_button.setVisible(enrolled and (not signed or not self.first_sync_required))
        self.signout.setEnabled(enrolled and not busy and signed)
        self.signout.setVisible(enrolled and signed)
        self.conflict_button.setEnabled(enrolled and not busy)
        self.close_button.setEnabled(not busy or connecting)
        self.sync_state.busy = busy
        self.sync_state.connecting = connecting
        self.sync_state.connected = signed
        self._show_state()
        self._sync_parent_state()

    def _show_state(self):
        if self.preview is None:
            return
        enrolled = self.preview.enrolled and self.preview.bootstrap_required_count == 0
        if not enrolled:
            self.state_label.setText("Sync not set up")
            self.summary_label.setText("Prepare this journal on this computer for future sync. Nothing will be uploaded yet.")
            self.details.setText(enrollment_preview_text(self.preview))
            self.account_label.hide()
            self.last_sync_label.hide()
            self.conflict_button.hide()
            return

        if self.sync_state.connecting:
            label = "Connecting…"
        elif self.sync_state.busy:
            label = "Syncing…"
        elif self.sync_state.conflicts > 0:
            label = "Needs attention"
        elif self.sync_state.failure == "offline":
            label = "Offline"
        elif self.sync_state.failure in {"auth", "cancelled"}:
            label = "Not connected"
        elif self.sync_state.failure:
            label = "Needs attention"
        elif self.auth is not None:
            label = "Up to date" if (self.sync_state.successful and not self.first_sync_required) else "Connected"
        elif self.disconnected or self.last_sync is not None:
            label = "Not connected"
        else:
            label = "Journal ready for sync"

        self.state_label.setText(label)
        email = getattr(self.auth, "email", None) if self.auth else None
        self.account_label.setText(f"Google account: {email}" if email else "Connected with Google" if self.auth else "")
        self.account_label.setVisible(self.auth is not None)

        if self.last_sync is not None:
            formatted = to_local_datetime(self.last_sync, self.zone).strftime("%b %d, %Y · %I:%M %p")
            self.last_sync_label.setText(f"Last synced: {formatted}")
            self.last_sync_label.setVisible(True)
        elif self.auth is not None:
            self.last_sync_label.setText("Never synced")
            self.last_sync_label.setVisible(True)
        else:
            self.last_sync_label.setText("Never synced")
            self.last_sync_label.setVisible(False)

        if self.auth is not None and self.sync_preview is not None:
            if self.first_sync_required:
                self.summary_label.setText("Zymptom is ready to upload this journal for the first time.")
                self.details.setText(first_sync_preview_text(self.sync_preview))
            else:
                self.summary_label.setText("Manual sync shares changes between this journal and your cloud mailbox.")
                self.details.setText(production_sync_details_text(self.sync_preview))
        elif self.disconnected:
            self.summary_label.setText("Connect with Google to manually sync this journal. Your local entries remain available.")
            self.details.setText(enrollment_preview_text(self.preview))
        else:
            self.summary_label.setText("Your existing entries have been prepared. Nothing has been uploaded yet.")
            self.details.setText(enrollment_preview_text(self.preview))

        self.conflict_button.setVisible(self.sync_state.conflicts > 0)
        if self.sync_state.conflicts > 0 and not self.sync_state.busy:
            detail = MESSAGES.get(self.sync_state.failure, "")
            self.status.setText(
                "An entry changed differently on two devices. Review the versions to continue."
                + (" " + detail if detail else "")
            )
        elif self.sync_state.failure:
            self.status.setText(MESSAGES.get(self.sync_state.failure, MESSAGES["other"]))

    def _error(self, error):
        self.status.setText(enrollment_failure_message(error))
        self.status.setFocus()

    def confirm_enrollment(self):
        if self.confirmation is not None:
            return
        self.refresh(clear_status=True)
        if self.preview is None:
            return
        if self.preview.enrolled and self.preview.bootstrap_required_count == 0:
            self.status.setText("This journal is already prepared for sync. Nothing was changed.")
            self.prepare_button.setEnabled(False)
            return
        confirmation = QMessageBox(QMessageBox.Icon.Information, "Prepare journal for sync?", "", parent=self)
        confirmation.setTextFormat(Qt.TextFormat.PlainText)
        confirmation.setText(
            "Prepare this journal for future sync?\n\n"
            f"• Existing entries will be preserved ({self.preview.entry_count} total)\n"
            f"• Local sync history will be created ({self.preview.bootstrap_required_count} to prepare, "
            f"{self.preview.already_tracked_count} already carrying history)\n"
            "• A verified safety backup will be created first\n"
            "• Nothing will be uploaded yet"
        )
        confirmation.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirmation.button(QMessageBox.StandardButton.Yes).setText("Prepare journal for sync")
        confirmation.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirmation.setEscapeButton(QMessageBox.StandardButton.Cancel)
        self.confirmation = confirmation
        confirmation.finished.connect(self._confirmed)
        confirmation.finished.connect(confirmation.deleteLater)
        self.prepare_button.setEnabled(False)
        confirmation.open()

    def _confirmed(self, result):
        self.confirmation = None
        if result != QMessageBox.StandardButton.Yes or self.preview is None:
            self.status.setText("Sync setup cancelled. Your journal was not changed.")
            self.prepare_button.setEnabled(
                self.preview is not None
                and (not self.preview.enrolled or self.preview.bootstrap_required_count > 0)
                and self.session.connection is not None
            )
            return
        self.prepare_button.setEnabled(False)
        try:
            self.session.enroll_sync()
            self.refresh(clear_status=False)
            self.status.setText("Journal prepared for sync. A verified safety backup was created. Nothing has been uploaded yet.")
            self.enrolled.emit()
        except (BackupError, SyncError, sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            self._error(exc)
            self.prepare_button.setEnabled(
                self.preview is not None
                and (not self.preview.enrolled or self.preview.bootstrap_required_count > 0)
                and self.session.connection is not None
            )

    def _available(self) -> bool:
        if self.worker is not None or self.sync_state.busy:
            return False
        if self.preview is None or not self.preview.enrolled or self.preview.bootstrap_required_count > 0:
            self.status.setText("Prepare this journal for sync before connecting.")
            return False
        parent = self.parent()
        if parent is not None and (
            getattr(parent, "editor", None) is not None
            or getattr(parent, "export_dialog", None) is not None
            or getattr(parent, "backup_dialog", None) is not None
            or getattr(parent, "recovery_dialog", None) is not None
        ):
            self.status.setText("Close the current editor or dialog before syncing.")
            return False
        return True

    def begin_signin(self):
        if not self._available():
            return
        try:
            self.config = read_config()
        except AuthError:
            self.sync_state.failure = "configuration"
            self._controls()
            return
        self.sync_state.failure = None
        self.status.clear()
        self.worker = ProductionAuthWorker(self.config, self)
        self.worker.success.connect(self._signed_in)
        self.worker.failure.connect(self._worker_failure)
        self.worker.finished.connect(self._worker_finished)
        self._controls()
        self.worker.start()

    def _signed_in(self, session):
        if isinstance(self.worker, ProductionAuthWorker) and self.worker.cancel.is_set():
            return
        self.auth = session
        self.disconnected = False
        self.sync_state.failure = None
        self.sync_state.successful = False
        journal_id = self.preview.journal_id if self.preview is not None else self.session.journal_id
        self.last_sync = self._load_saved_last_sync(journal_id)
        self.refresh(clear_status=True)
        if self.first_sync_required:
            self.status.setText("Review the first-sync details above, then select Start sync when ready.")
        else:
            self.status.setText("Connected. Select Sync now to manually synchronize this journal.")

    def cancel_auth(self):
        if isinstance(self.worker, ProductionAuthWorker):
            self.worker.cancel.set()
            self.status.setText("Cancelling sign-in…")

    def sign_out(self):
        if self.worker is not None or self.sync_state.busy:
            return
        self.auth = None
        self.config = None
        self.last_sync = None
        self.disconnected = True
        self.sync_state.connected = False
        self.sync_state.successful = False
        self.sync_state.failure = None
        self._controls()
        self.status.setText(
            "Disconnected from Google. Your entries, sync history, journal identity, and cloud data were kept. Connect again anytime."
        )

    disconnect_sync = sign_out

    def _build_mailbox(self, journal_id):
        if self.mailbox_factory is not None:
            return self.mailbox_factory(self.config, self.auth, journal_id)
        if self.config is None or self.auth is None:
            raise AuthError("Sign in before syncing.", kind="auth")
        return FirestoreMailbox(self.config.firebase_project_id, self.auth, journal_id=journal_id)

    def start_first_sync(self):
        if not self.auth or not self._available():
            return
        self._execute_sync(approve_first_sync=True)

    def begin_sync(self):
        if not self.auth or not self._available():
            return
        if self.first_sync_required:
            self.status.setText("Review and approve the first production sync using Start sync before syncing.")
            return
        self._execute_sync(approve_first_sync=False)

    def _execute_sync(self, *, approve_first_sync: bool):
        self.sync_state.failure = None
        self.sync_state.busy = True
        self.status.clear()
        self._controls()
        try:
            journal_id = self.preview.journal_id if self.preview is not None else self.session.journal_id
            mailbox = self._build_mailbox(journal_id)
            result = self.session.sync_production(
                mailbox,
                approve_first_sync=approve_first_sync,
                has_synced_before=(self.last_sync is not None),
            )
            self._sync_finished(result)
        except (
            AuthError,
            TransportError,
            IsolationError,
            BackupError,
            SyncError,
            sqlite3.Error,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            self._worker_failure(failure_kind(exc))

    def _sync_finished(self, result):
        self.sync_state.busy = False
        self.sync_state.failure = None
        self.sync_state.conflicts = result.conflicts
        self.sync_state.successful = result.up_to_date
        self.first_sync_required = False
        if result.conflicts == 0:
            self.last_sync = self.now()
            self._save_last_sync(result.journal_id, self.last_sync)
        parent = self.parent()
        if parent is not None and hasattr(parent, "refresh"):
            parent.refresh()
            if hasattr(parent, "history"):
                parent.history.refresh()
        self.refresh(clear_status=True)
        if result.up_to_date:
            self.status.setText("Journal synchronized and up to date.")
        self.synced.emit()

    def _worker_failure(self, kind):
        valid = {"offline", "auth", "permission", "integrity", "backup", "signin", "configuration", "cancelled", "other"}
        self.sync_state.busy = False
        self.sync_state.connecting = False
        self.sync_state.failure = kind if kind in valid else "other"
        self.sync_state.successful = False
        if self.sync_state.failure == "auth":
            self.auth = None
        self._controls()

    def _worker_finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            if worker.cancel.is_set() and self.auth is None:
                self.sync_state.failure = "cancelled"
            worker.deleteLater()
        self.sync_state.busy = False
        self.sync_state.connecting = False
        self.refresh(clear_status=False)

    def inspect_conflicts(self):
        if not self._available():
            return
        service = SyncService(self.session.episodes, before_merge=self.session.before_destructive_change)
        conflicts = service.unresolved_conflicts()
        if not conflicts:
            self.refresh(clear_status=False)
            return
        conflict = conflicts[0]
        dialog = QDialog(self)
        dialog.setWindowTitle("Review conflict")
        layout = QVBoxLayout(dialog)
        deleted = [candidate.deleted for candidate in conflict.candidates]
        explanation = QLabel(
            "This entry was deleted on all devices. Choose a version to confirm the deletion."
            if all(deleted)
            else "This entry was deleted on one device but changed on another."
            if any(deleted)
            else "This entry changed differently on two devices. Choose the version you want to keep."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        versions = QHBoxLayout()

        def resolve(candidate):
            try:
                service.resolve(conflict.record_id, chosen_change_id=candidate.change_id)
                dialog.accept()
                parent = self.parent()
                if parent is not None and hasattr(parent, "refresh"):
                    parent.refresh()
                    if hasattr(parent, "history"):
                        parent.history.refresh()
                self.sync_state.successful = False
                self.sync_state.failure = None
                self.refresh(clear_status=True)
                self.status.setText("Version saved. Sync now to share it.")
            except (SyncError, BackupError, sqlite3.Error, OSError, ValueError, RuntimeError):
                QMessageBox.warning(dialog, "Resolution failed", "The conflict was not resolved. Inspect it again and retry.")

        for index, candidate in enumerate(conflict.candidates):
            column = QVBoxLayout()
            column.addWidget(QLabel(f"Version {index + 1}"))
            view = QTextEdit()
            view.setReadOnly(True)
            view.setAccessibleName(f"Version {index + 1} details")
            view.setPlainText(candidate_text(candidate, self.zone))
            column.addWidget(view)
            choose = QPushButton(
                "Keep this version" if index == 0 else "Keep the other version" if index == 1 else f"Keep version {index + 1}"
            )
            choose.clicked.connect(lambda _checked=False, selected=candidate: resolve(selected))
            column.addWidget(choose)
            versions.addLayout(column)
        layout.addLayout(versions)
        dialog.resize(800, 550)
        dialog.finished.connect(dialog.deleteLater)
        dialog.exec()

    def done(self, result):
        if isinstance(self.worker, ProductionAuthWorker):
            self.worker.cancel.set()
            self.worker.wait(1000)
        if self.confirmation is not None:
            self.confirmation.reject()
        self._sync_parent_state()
        super().done(result)
