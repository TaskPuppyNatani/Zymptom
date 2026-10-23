"""Today and History, sharing the existing episode editor."""

import sqlite3
from datetime import UTC, datetime

from PySide6.QtCore import QTimeZone, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from zymptom.services import EpisodeService

from .editor import EpisodeEditor
from .history import HistoryView
from .episode_context_menu import connect_episode_context_menu
from .branding import application_icon, brand_mark
from .labels import OUTCOMES
from .time import to_local
from .widgets import comfortable


class MainWindow(QMainWindow):
    def __init__(self, service: EpisodeService, *, zone: QTimeZone | None = None, now=None):
        super().__init__()
        self.service = service
        self.zone = zone or QTimeZone.systemTimeZone()
        self.now = now or (lambda: datetime.now(UTC))
        self.editor = None
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
        self.history.episode_activated.connect(self.open_episode_id)
        self.history.episode_delete_requested.connect(self.delete_episode_from_context_menu)
        self.new_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        self.new_shortcut.activated.connect(self.new_episode)
        self.day_timer = QTimer(self)
        self.day_timer.setInterval(60_000)
        self.day_timer.timeout.connect(self.refresh)
        self.day_timer.start()
        self.refresh()
        self.log_button.setFocus()

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

    def _open_editor(self, episode=None):
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
