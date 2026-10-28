"""Today and History, sharing the existing episode editor."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QTimeZone, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from zymptom.services import EpisodeService
from zymptom.persistence.backups import BackupError
from zymptom.services.journal import ManualBackupError

from .editor import EpisodeEditor
from .history import HistoryView
from .episode_context_menu import connect_episode_context_menu
from .branding import application_icon, brand_mark
from .export_dialog import ExportDialog
from .backup_dialog import BackupDialog
from .recovery_dialog import RecoveryDialog
from .sync_dialog import SyncDialog
from .labels import OUTCOMES
from .time import to_local, to_local_datetime
from .widgets import comfortable


class MainWindow(QMainWindow):
    def __init__(self, service: EpisodeService, *, zone: QTimeZone | None = None, now=None, backup_session=None):
        super().__init__()
        self.service = service
        self.zone = zone or QTimeZone.systemTimeZone()
        self.now = now or (lambda: datetime.now(UTC))
        self.editor = None
        self.export_dialog = None
        self.backup_session = backup_session
        self.backup_dialog = None
        self.recovery_dialog = None
        self.sync_dialog = None
        self.backup_action = self.menuBar().addMenu("Journal").addAction("Backups / Restore…")
        self.backup_action.setEnabled(backup_session is not None)
        self.backup_action.triggered.connect(self.open_backup_dialog)
        self.settings_menu = self.menuBar().addMenu("Settings")
        self.sync_action = self.settings_menu.addAction("Sync…")
        self.sync_action.setEnabled(backup_session is not None)
        self.sync_action.triggered.connect(self.open_sync_dialog)
        self.data_menu = self.settings_menu.addMenu("Data")
        self.manual_backup_action = self.data_menu.addAction("Back Up Journal…")
        self.manual_backup_action.setEnabled(backup_session is not None)
        self.manual_backup_action.triggered.connect(self.back_up_journal)
        self.recovery_action = self.data_menu.addAction("Import / Recover Journal…")
        self.recovery_action.setEnabled(backup_session is not None)
        self.recovery_action.triggered.connect(self.open_recovery_dialog)
        self.statusBar().setAccessibleName("Journal backup status")
        self.setWindowTitle("Zymptom — Today")
        self.setWindowIcon(application_icon())
        self.resize(640, 640)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self.heading = QLabel("Today")
        heading_font = self.heading.font()
        heading_font.setBold(True)
        self.heading.setFont(heading_font)
        layout.addWidget(self.heading)
        self.day_label = QLabel()
        self.day_label.setProperty("tone", "secondary")
        layout.addWidget(self.day_label)
        navigation = QHBoxLayout()
        self.today_button = QPushButton("Today")
        self.history_button = QPushButton("History")
        self.navigation_group = QButtonGroup(self)
        for button in (self.today_button, self.history_button):
            comfortable(button, f"Show {button.text()}")
            button.setCheckable(True)
            self.navigation_group.addButton(button)
            navigation.addWidget(button)
        self.export_button = QPushButton("Export / Summary…")
        comfortable(self.export_button, "Export episodes and view deterministic summary")
        navigation.addWidget(self.export_button)
        self.today_button.setChecked(True)
        layout.addLayout(navigation)
        self.log_button = QPushButton("+ Log Something")
        self.log_button.setProperty("primary", True)
        comfortable(self.log_button, "Log something")
        self.log_button.setToolTip("Log an attempt (Ctrl+N)")
        layout.addWidget(self.log_button)
        self.pages = QStackedWidget()
        self.today_page = QWidget()
        today_layout = QVBoxLayout(self.today_page)
        today_layout.setContentsMargins(0, 0, 0, 0)
        self.empty_label = QLabel("No entries today. Start with something you tried to do.")
        self.empty_label.setWordWrap(True)
        self.empty_label.setProperty("tone", "secondary")
        today_layout.addWidget(self.empty_label)
        self.entries = QListWidget()
        self.entries.setObjectName("episodes")
        self.entries.setAccessibleName("Today's episodes. Select an entry to edit it.")
        self.entries.setWordWrap(True)
        self.entries.setSpacing(6)
        connect_episode_context_menu(
            self.entries,
            on_edit=self.open_episode_id,
            on_delete=self.delete_episode_from_context_menu,
        )
        today_layout.addWidget(self.entries, 1)
        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setAccessibleName("Today error")
        self.error.setWordWrap(True)
        self.error.hide()
        today_layout.addWidget(self.error)
        self.retry_button = QPushButton("Retry loading Today")
        comfortable(self.retry_button, "Retry loading Today")
        self.retry_button.hide()
        today_layout.addWidget(self.retry_button)
        self.pages.addWidget(self.today_page)
        self.history = HistoryView(service, zone=self.zone, now=lambda: self.now(), parent=self)
        self.pages.addWidget(self.history)
        layout.addWidget(self.pages, 1)
        self.branding = brand_mark(content)
        layout.addWidget(self.branding)
        self.setCentralWidget(content)
        self.log_button.clicked.connect(self.new_episode)
        self.entries.itemClicked.connect(self.open_episode)
        self.entries.itemActivated.connect(self.open_episode)
        self.retry_button.clicked.connect(self.refresh)
        self.today_button.clicked.connect(self.show_today)
        self.history_button.clicked.connect(self.show_history)
        self.export_button.clicked.connect(self.open_export_dialog)
        self.history.episode_activated.connect(self.open_episode_id)
        self.history.episode_delete_requested.connect(self.delete_episode_from_context_menu)
        self.new_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        self.new_shortcut.activated.connect(self.new_episode)
        self.day_timer = QTimer(self)
        self.day_timer.setInterval(60_000)
        self.day_timer.timeout.connect(self.refresh)
        self.day_timer.timeout.connect(self._daily_backup)
        self.day_timer.start()
        self.refresh()
        self.log_button.setFocus()
        if backup_session is not None:
            QTimer.singleShot(0, self._daily_backup)

    def _daily_backup(self):
        if self.backup_session is None or self.backup_session.connection is None:
            return
        try:
            info = self.backup_session.backups.daily_if_changed(
                self.backup_session.connection, now=to_local_datetime(self.now(), self.zone),
            )
            if info is not None:
                self.statusBar().showMessage("Automatic backup created and verified. " + self.backup_session.backups.warning)
        except (BackupError, OSError, sqlite3.Error):
            self.statusBar().showMessage("Automatic backup failed. Open Journal → Backups / Restore to retry and check the backup folder.")

    def _sync_modal_open(self) -> bool:
        return isinstance(self.sync_dialog, SyncDialog)

    @property
    def enrollment_dialog(self):
        return self.sync_dialog if isinstance(self.sync_dialog, SyncDialog) else None

    def open_backup_dialog(self):
        if self.backup_session is None:
            return
        if self.editor is not None or self.export_dialog is not None or self.recovery_dialog is not None or self._sync_modal_open():
            self.statusBar().showMessage("Finish or close the current editor or export dialog before managing backups.")
            return
        if self.backup_dialog is not None:
            self.backup_dialog.raise_()
            return
        dialog = BackupDialog(self.backup_session, zone=self.zone, now=lambda: self.now(), parent=self)
        self.backup_dialog = dialog
        dialog.restored.connect(self._restored_backup)
        dialog.finished.connect(lambda: setattr(self, "backup_dialog", None))
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()

    def back_up_journal(self):
        if self.backup_session is None or self.backup_session.connection is None:
            return
        if self.editor is not None or self.export_dialog is not None or self.backup_dialog is not None or self.recovery_dialog is not None or self._sync_modal_open():
            self.statusBar().showMessage("Close the current editor or dialog before backing up the journal.")
            return
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        directory = Path(documents) if documents and Path(documents).is_dir() else Path.home()
        filename = f"Zymptom-Journal-{to_local(self.now(), self.zone).toString('yyyy-MM-dd')}.sqlite3"
        path, _ = QFileDialog.getSaveFileName(
            self, "Back Up Journal", str(directory / filename), "Journal files (*.sqlite3);;All files (*)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        destination = Path(path)
        overwrite = destination.exists() or destination.is_symlink()
        if overwrite and QMessageBox.question(
            self, "Replace existing file?", f"Replace the existing file {destination.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backup_session.create_manual_backup(destination, overwrite=overwrite)
        except ManualBackupError as exc:
            messages = {
                "current": "Choose a different location. Zymptom cannot overwrite the journal currently in use.",
                "verify": "The backup could not be verified, so Zymptom did not report it as successful.",
                "exists": "A file already exists at that location. Choose another name and try again.",
            }
            QMessageBox.warning(self, "Backup not created", messages.get(exc.kind, "Zymptom could not create the journal backup. Check the location and try again."))
            return
        QMessageBox.information(self, "Journal backup", "Journal backup created successfully.")

    def open_recovery_dialog(self):
        if self.backup_session is None or self.backup_session.connection is None:
            return
        if self.editor is not None or self.export_dialog is not None or self.backup_dialog is not None or self._sync_modal_open():
            self.statusBar().showMessage("Close the current editor or dialog before recovering a journal.")
            return
        if self.recovery_dialog is not None:
            self.recovery_dialog.raise_()
            return
        dialog = RecoveryDialog(self.backup_session, zone=self.zone, parent=self)
        self.recovery_dialog = dialog
        dialog.recovered.connect(self._restored_backup)
        dialog.finished.connect(lambda: setattr(self, "recovery_dialog", None))
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()

    def open_sync_dialog(self):
        if self.backup_session is None or self.backup_session.connection is None:
            return
        if self.editor is not None or self.export_dialog is not None or self.backup_dialog is not None or self.recovery_dialog is not None:
            self.statusBar().showMessage("Close the current editor or dialog before setting up sync.")
            return
        if self._sync_modal_open():
            self.sync_dialog.raise_()
            return
        dialog = SyncDialog(self.backup_session, parent=self)
        self.sync_dialog = dialog
        dialog.finished.connect(lambda: setattr(self, "sync_dialog", None))
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()

    open_enrollment_dialog = open_sync_dialog

    def _restored_backup(self):
        self.refresh()
        self.history.refresh()

    def closeEvent(self, event):
        if self.backup_dialog is not None:
            self.backup_dialog.reject()
        if self.recovery_dialog is not None:
            self.recovery_dialog.reject()
        if self._sync_modal_open():
            self.sync_dialog.reject()
        super().closeEvent(event)

    def refresh(self):
        current = to_local(self.now(), self.zone)
        self.day_label.setText(current.toString("dddd, MMMM d, yyyy"))
        try:
            episodes = [
                episode for episode in self.service.list()
                if to_local(episode.occurred_at, self.zone).date() == current.date()
            ]
        except (sqlite3.Error, OSError, ValueError):
            self.empty_label.hide()
            self._load_error("Could not load today's entries. Previously loaded entries may be out of date. Please retry.")
            return
        selected = self.entries.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.entries.clear()
        for episode in episodes:
            local = to_local(episode.occurred_at, self.zone)
            details = []
            if episode.duration_minutes is not None:
                details.append(f"{episode.duration_minutes} min")
            details.append(OUTCOMES[episode.outcome])
            if episode.recovery_minutes is not None:
                details.append(f"Recovery {episode.recovery_minutes} min")
            item = QListWidgetItem(f"{local.toString('h:mm AP')}\n{episode.activity}\n{' · '.join(details)}")
            item.setData(Qt.ItemDataRole.UserRole, str(episode.id))
            self.entries.addItem(item)
            if str(episode.id) == selected_id:
                self.entries.setCurrentItem(item)
        self.empty_label.setVisible(not episodes)
        self.error.hide()
        self.retry_button.hide()

    def _load_error(self, text):
        self.error.setText(text)
        self.error.show()
        self.retry_button.show()

    def new_episode(self):
        if self.backup_dialog is not None or self.recovery_dialog is not None or self._sync_modal_open():
            return
        self._open_editor()

    def open_episode(self, item):
        self.open_episode_id(item.data(Qt.ItemDataRole.UserRole))

    def open_episode_id(self, episode_id):
        try:
            episode = self.service.get(episode_id)
        except (sqlite3.Error, OSError, ValueError):
            self._open_error("Could not open this entry. Please retry.")
            return
        if episode is None:
            self._refresh_views()
            self._open_error("This entry is no longer available. Entries have been refreshed.")
            return
        self._open_editor(episode)

    def delete_episode_from_context_menu(self, episode_id):
        self.open_episode_id(episode_id)
        editor = self.editor
        if editor is not None and editor.original is not None and str(editor.original.id) == str(episode_id):
            editor.delete_entry()

    def _open_error(self, message):
        if self.pages.currentWidget() is self.history:
            self.history.show_error(message)
        else:
            self._load_error(message)

    def _refresh_views(self):
        self.refresh()
        if self.pages.currentWidget() is self.history:
            self.history.refresh()

    def show_today(self):
        self.pages.setCurrentWidget(self.today_page)
        self.today_button.setChecked(True)
        self.heading.setText("Today")
        self.day_label.show()
        self.setWindowTitle("Zymptom — Today")
        self.refresh()

    def show_history(self):
        self.pages.setCurrentWidget(self.history)
        self.history_button.setChecked(True)
        self.heading.setText("History")
        self.day_label.hide()
        self.setWindowTitle("Zymptom — History")
        self.history.refresh()

    def open_export_dialog(self):
        if self.backup_dialog is not None or self.recovery_dialog is not None or self._sync_modal_open():
            return
        if self.backup_session is not None and self.backup_session.connection is None:
            self.statusBar().showMessage("The journal is unavailable. Close Zymptom and use the preserved safety backup.")
            return
        if self.export_dialog is not None:
            self.export_dialog.raise_()
            self.export_dialog.activateWindow()
            return
        dialog = ExportDialog(self.service, zone=self.zone, now=self.now(), parent=self)
        self.export_dialog = dialog
        dialog.finished.connect(self._export_dialog_finished)
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()

    def _export_dialog_finished(self):
        self.export_dialog = None

    def _open_editor(self, episode=None):
        if self.backup_dialog is not None or self.recovery_dialog is not None or self._sync_modal_open():
            return
        if self.backup_session is not None and self.backup_session.connection is None:
            self.statusBar().showMessage("The journal is unavailable. Close Zymptom and use the preserved safety backup.")
            return
        if self.editor is not None:
            self.editor.raise_()
            self.editor.activateWindow()
            return
        editor = EpisodeEditor(self.service, episode, zone=self.zone, now=self.now(), parent=self)
        self.editor = editor
        editor.accepted.connect(self._refresh_views)
        editor.deleted.connect(self.refresh)
        editor.deleted.connect(self.history.refresh)
        editor.finished.connect(self._editor_finished)
        editor.finished.connect(editor.deleteLater)
        editor.open()

    def _editor_finished(self):
        self.editor = None
        self.log_button.setFocus()
        self._daily_backup()
