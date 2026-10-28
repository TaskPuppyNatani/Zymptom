"""Inspect local Z5 readiness and explicitly enroll the normal production journal."""

import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from zymptom.persistence.backups import BackupError
from zymptom.sync.contract import SyncError

from .branding import brand_mark
from .sync_presentation import enrollment_failure_message, enrollment_preview_text
from .widgets import comfortable


class SyncDialog(QDialog):
    enrolled = Signal()

    def __init__(self, session, *, parent=None):
        super().__init__(parent)
        self.session = session
        self.preview = None
        self.confirmation = None
        self.setWindowTitle("Sync")
        self.setAccessibleName("Production journal sync setup")
        self.setModal(True)
        self.resize(560, 400)
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
        layout.addWidget(self.status)
        layout.addStretch(1)
        actions = QHBoxLayout()
        self.close_button = QPushButton("Close")
        self.cancel_button = self.close_button
        self.prepare_button = QPushButton("Prepare journal for sync…")
        comfortable(self.close_button, "Close sync setup")
        comfortable(self.prepare_button, "Prepare journal for sync")
        self.prepare_button.setProperty("primary", True)
        self.close_button.clicked.connect(self.reject)
        self.prepare_button.clicked.connect(self.confirm_enrollment)
        actions.addWidget(self.close_button)
        actions.addWidget(self.prepare_button)
        layout.addLayout(actions)
        layout.addWidget(brand_mark(self))
        self.refresh()
        if self.prepare_button.isEnabled():
            self.prepare_button.setFocus()
        else:
            self.close_button.setFocus()

    def refresh(self, *, clear_status: bool = True):
        self.preview = None
        if clear_status:
            self.status.clear()
        try:
            preview = self.session.inspect_enrollment()
            self.preview = preview
            if preview.enrolled:
                self.state_label.setText("Journal ready for sync")
                self.summary_label.setText("Your existing entries have been prepared. Nothing has been uploaded yet.")
            else:
                self.state_label.setText("Sync not set up")
                self.summary_label.setText("Prepare this journal on this computer for future sync. Nothing will be uploaded yet.")
            self.details.setText(enrollment_preview_text(preview))
            self.prepare_button.setEnabled(
                (not preview.enrolled or preview.bootstrap_required_count > 0)
                and self.confirmation is None
                and self.session.connection is not None
            )
        except (BackupError, SyncError, sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            self.state_label.setText("Needs attention")
            self.summary_label.setText("Your local entries remain available on this computer.")
            self.details.setText("Sync readiness could not be verified.")
            self.prepare_button.setEnabled(False)
            self._error(exc)

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

    def done(self, result):
        if self.confirmation is not None:
            self.confirmation.reject()
        super().done(result)
