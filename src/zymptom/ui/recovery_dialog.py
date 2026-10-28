"""Inspect a separate journal and explicitly confirm local recovery."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from zymptom.persistence.backups import BackupError
from zymptom.services.journal import RecoveryError

from .time import to_local
from .widgets import comfortable


class RecoveryDialog(QDialog):
    recovered = Signal()

    def __init__(self, session, *, zone, parent=None):
        super().__init__(parent)
        self.session = session
        self.zone = zone
        self.preview = None
        self.confirmation = None
        self.setWindowTitle("Import / Recover Journal")
        self.setAccessibleName("Inspect and recover a journal")
        self.setModal(True)
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        intro = QLabel("Choose a Zymptom journal to inspect. Recovery replaces the current journal after a safety backup.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.choose_button = QPushButton("Choose journal file…")
        comfortable(self.choose_button, "Choose journal file")
        self.choose_button.clicked.connect(self.choose_file)
        layout.addWidget(self.choose_button)
        self.details = QLabel("No journal selected.")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setAccessibleName("Journal inspection")
        layout.addWidget(self.details)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Recovery status")
        self.status.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.status)
        layout.addStretch(1)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel")
        self.recover_button = QPushButton("Recover this journal…")
        comfortable(self.cancel_button, "Cancel recovery")
        comfortable(self.recover_button, "Recover this journal")
        self.recover_button.setProperty("primary", True)
        self.recover_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.reject)
        self.recover_button.clicked.connect(self.confirm_recovery)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.recover_button)
        layout.addLayout(actions)
        self.choose_button.setFocus()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose journal file", str(self.session.database.parent), "Journal files (*.sqlite3 *.sqlite *.db);;All files (*)"
        )
        if not path:
            return
        self.inspect_file(path)

    def inspect_file(self, path):
        self.preview = None
        self.recover_button.setEnabled(False)
        self.status.clear()
        try:
            preview = self.session.inspect_recovery(path)
            self.preview = preview
            dates = (f"{to_local(preview.oldest, self.zone).toString('MMM d, yyyy')} – {to_local(preview.newest, self.zone).toString('MMM d, yyyy')}"
                     if preview.oldest is not None else "No entries recorded")
            self.details.setText(
                f"{preview.path.name}\nValid Zymptom journal · Integrity verified\n"
                f"Entries: {preview.entry_count}\nDate range: {dates}\n"
                f"Journal version: {preview.schema_version}\n"
                f"{'Upgrade required during recovery' if preview.migration_required else 'No upgrade required'}"
            )
            self.recover_button.setEnabled(True)
        except (RecoveryError, OSError) as exc:
            self.details.setText("No valid journal selected.")
            self._error(exc)

    def _error(self, error):
        messages = {
            "newer": "This journal was created by a newer version of Zymptom and cannot be opened safely.",
            "current": "Choose a journal other than the one currently open.",
            "open": "Close the selected journal in its other application before trying again.",
            "changed": "The selected journal changed. Inspect it again before recovery.",
            "backup": "Zymptom could not create a safety backup, so your current journal was not changed.",
            "migration": "Zymptom could not upgrade the selected journal. Your current journal was not changed.",
            "stage": "Zymptom could not prepare the selected journal. Your current journal was not changed.",
            "unavailable": "The current journal is unavailable. Close Zymptom and check your backups.",
            "sandbox": "This journal contains sandbox sync state and cannot be recovered here.",
        }
        if self.session.connection is None:
            message = "The journal could not be reopened. Close Zymptom; the safety backup is preserved in the backup folder."
        else:
            message = messages.get(getattr(error, "kind", None), "This file does not appear to be a valid Zymptom journal, or it may be damaged.")
        self.status.setText(message)
        self.status.setFocus()

    def confirm_recovery(self):
        if self.preview is None or self.confirmation is not None:
            return
        confirmation = QMessageBox(QMessageBox.Icon.Warning, "Recover this journal?", "", parent=self)
        confirmation.setTextFormat(Qt.TextFormat.PlainText)
        confirmation.setText(
            f"Recovering {self.preview.path.name} ({self.preview.entry_count} entries) will replace the current journal.\n\n"
            "A verified safety backup of the current journal will be created first."
        )
        confirmation.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirmation.button(QMessageBox.StandardButton.Yes).setText("Recover Journal")
        confirmation.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirmation.setEscapeButton(QMessageBox.StandardButton.Cancel)
        self.confirmation = confirmation
        confirmation.finished.connect(self._confirmed)
        confirmation.finished.connect(confirmation.deleteLater)
        confirmation.open()

    def _confirmed(self, result):
        self.confirmation = None
        if result != QMessageBox.StandardButton.Yes or self.preview is None:
            self.status.setText("Recovery cancelled. The journal is unchanged.")
            return
        self.choose_button.setEnabled(False)
        self.recover_button.setEnabled(False)
        plan = None
        try:
            plan = self.session.prepare_recovery(self.preview)
            self.session.restore(plan)
            self.status.setText("Journal recovered successfully. A safety backup is preserved.")
            self.preview = None
            self.details.setText("Recovery complete.")
            self.recovered.emit()
        except (BackupError, OSError) as exc:
            if isinstance(exc, RecoveryError):
                self._error(exc)
            elif self.session.connection is None and plan is not None:
                self.status.setText(f"The journal could not be reopened. Close Zymptom. The safety backup is preserved at:\n{plan.safety.path}")
                self.status.setFocus()
            elif self.session.connection is None:
                self._error(exc)
            else:
                self.status.setText("Recovery failed. The current journal was not changed or was restored from its safety backup.")
                self.status.setFocus()
        finally:
            self.choose_button.setEnabled(self.session.connection is not None)
            self.recover_button.setEnabled(self.preview is not None and self.session.connection is not None)

    def done(self, result):
        if self.confirmation is not None:
            self.confirmation.reject()
        self.session.cancel_restore()
        super().done(result)
