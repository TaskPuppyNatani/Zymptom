"""Small local backup catalog and explicit, safety-backed restore confirmation."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout

from zymptom.persistence.backups import BackupError

from .branding import brand_mark
from .time import to_local
from .widgets import comfortable

KIND_LABELS = {"manual": "Manual", "daily": "Automatic daily", "migration": "Before migration", "pre-restore": "Before restore", "safety": "Safety"}


class BackupDialog(QDialog):
    restored = Signal()

    def __init__(self, session, *, zone, now, parent=None):
        super().__init__(parent)
        self.session = session
        self.zone = zone
        self.now = now
        self.plan = None
        self.confirmation = None
        self.setWindowTitle("Backups / Restore")
        self.setAccessibleName("Local journal backups and restore")
        self.setModal(True)
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        intro = QLabel("Backups stay on this computer. Restore replaces the current journal after creating a verified safety backup.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.entries = QListWidget()
        self.entries.setAccessibleName("Available journal backups, newest first")
        self.entries.setWordWrap(True)
        self.entries.setSpacing(6)
        layout.addWidget(self.entries, 1)
        location = QLabel(f"Backup folder: {session.backups.directory}")
        location.setTextFormat(Qt.TextFormat.PlainText)
        location.setWordWrap(True)
        location.setProperty("tone", "secondary")
        layout.addWidget(location)
        self.status = QLabel()
        self.status.setAccessibleName("Backup status")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.manual_button = QPushButton("Create backup now")
        self.restore_button = QPushButton("Restore selected…")
        self.refresh_button = QPushButton("Refresh list")
        self.close_button = QPushButton("Close")
        for button in (self.manual_button, self.restore_button, self.refresh_button, self.close_button):
            comfortable(button, button.text().replace("…", ""))
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addWidget(brand_mark(self))
        self.manual_button.clicked.connect(self.create_backup)
        self.manual_button.setEnabled(session.connection is not None)
        self.restore_button.clicked.connect(self.prepare_restore)
        self.refresh_button.clicked.connect(self.refresh)
        self.close_button.clicked.connect(self.reject)
        self.entries.currentItemChanged.connect(self._selection_changed)
        self.refresh()
        self.manual_button.setFocus()

    def _selection_changed(self, *args):
        item = self.entries.currentItem()
        self.restore_button.setEnabled(
            item is not None and item.data(Qt.ItemDataRole.UserRole).valid
            and self.confirmation is None and self.session.connection is not None
        )

    def refresh(self):
        selected = self.entries.currentItem()
        path = selected.data(Qt.ItemDataRole.UserRole).path if selected else None
        try:
            backups = self.session.backups.list()
        except (OSError, BackupError):
            self.status.setText("Could not read the backup folder. Check its permissions and try Refresh list.")
            return
        self.entries.clear()
        for info in backups:
            when = to_local(info.created_at, self.zone).toString("MMM d, yyyy  h:mm:ss AP")
            validation = f"Verified · schema {info.schema_version}" if info.valid else f"Cannot restore: {info.error}"
            item = QListWidgetItem(f"{when} · {KIND_LABELS[info.kind]} · {info.size:,} bytes\n{validation}\n{info.path.name}")
            item.setData(Qt.ItemDataRole.UserRole, info)
            self.entries.addItem(item)
            if info.path == path:
                self.entries.setCurrentItem(item)
        self._selection_changed()

    def create_backup(self):
        if self.session.connection is None:
            self.status.setText("The journal is unavailable. Close Zymptom; the safety backups remain in the listed folder.")
            return
        try:
            info = self.session.backups.create(self.session.connection, "manual", now=self.now())
            self.refresh()
            self.status.setText(f"Backup created and verified: {info.path.name}" + (f"\n{self.session.backups.warning}" if self.session.backups.warning else ""))
        except (BackupError, OSError) as exc:
            self.status.setText(f"Backup failed: {exc}")
            self.status.setFocus()

    def prepare_restore(self):
        item = self.entries.currentItem()
        if item is None or self.confirmation is not None:
            return
        try:
            self.plan = self.session.prepare_restore(item.data(Qt.ItemDataRole.UserRole).path)
        except (BackupError, OSError) as exc:
            self.status.setText(f"Restore could not be prepared: {exc}")
            self.status.setFocus()
            self.refresh()
            return
        selected = self.plan.selected
        when = to_local(selected.created_at, self.zone).toString("MMM d, yyyy  h:mm:ss AP")
        confirmation = QMessageBox(QMessageBox.Icon.Warning, "Restore this backup?", "", parent=self)
        confirmation.setTextFormat(Qt.TextFormat.PlainText)
        confirmation.setText(
            f"Replace the entire current journal with this {KIND_LABELS[selected.kind].lower()} backup from {when}?\n\n"
            f"{selected.path.name}\n\nA verified safety backup of the current journal has been saved as:\n{self.plan.safety.path.name}"
        )
        confirmation.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirmation.button(QMessageBox.StandardButton.Yes).setText("Restore backup")
        confirmation.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirmation.setEscapeButton(QMessageBox.StandardButton.Cancel)
        self.confirmation = confirmation
        confirmation.finished.connect(self._confirmed)
        confirmation.finished.connect(confirmation.deleteLater)
        for button in (self.manual_button, self.restore_button, self.refresh_button):
            button.setEnabled(False)
        confirmation.open()

    def _confirmed(self, result):
        self.confirmation = None
        try:
            if result == QMessageBox.StandardButton.Yes:
                self.session.restore(self.plan)
                self.status.setText("Backup restored and verified. The pre-restore safety backup is preserved.")
                self.restored.emit()
            else:
                self.session.cancel_restore()
                self.status.setText("Restore cancelled. The journal is unchanged.")
        except (BackupError, OSError) as exc:
            self.status.setText(str(exc))
            self.status.setFocus()
        finally:
            self.plan = None
            self.manual_button.setEnabled(self.session.connection is not None)
            self.refresh_button.setEnabled(True)
            self.refresh()

    def done(self, result):
        if self.confirmation is not None:
            self.confirmation.reject()
        self.session.cancel_restore()
        self.plan = None
        super().done(result)
