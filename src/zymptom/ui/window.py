"""Today's local calendar day, with one action for logging an attempt."""

import sqlite3
from datetime import UTC, datetime

from PySide6.QtCore import QTimeZone, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QVBoxLayout, QWidget,
)

from zymptom.services import EpisodeService

from .editor import EpisodeEditor
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
        layout.addWidget(self.day_label)
        self.log_button = QPushButton("+ Log Something")
        comfortable(self.log_button, "Log something")
        self.log_button.setToolTip("Log an attempt (Ctrl+N)")
        layout.addWidget(self.log_button)
        self.empty_label = QLabel("No entries today. Start with something you tried to do.")
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)
        self.entries = QListWidget()
        self.entries.setAccessibleName("Today's episodes. Select an entry to edit it.")
        self.entries.setWordWrap(True)
        self.entries.setSpacing(6)
        layout.addWidget(self.entries, 1)
        self.error = QLabel()
        self.error.setAccessibleName("Today error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.retry_button = QPushButton("Retry loading Today")
        comfortable(self.retry_button, "Retry loading Today")
        self.retry_button.hide()
        layout.addWidget(self.retry_button)
        self.setCentralWidget(content)
        self.log_button.clicked.connect(self.new_episode)
        self.entries.itemClicked.connect(self.open_episode)
        self.entries.itemActivated.connect(self.open_episode)
        self.retry_button.clicked.connect(self.refresh)
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
        try:
            episode = self.service.get(item.data(Qt.ItemDataRole.UserRole))
        except (sqlite3.Error, OSError, ValueError):
            self._load_error("Could not open this entry. Please retry.")
            return
        if episode is None:
            self.refresh()
            self._load_error("This entry is no longer available. Today has been refreshed.")
            return
        self._open_editor(episode)

    def _open_editor(self, episode=None):
        if self.editor is not None:
            self.editor.raise_()
            self.editor.activateWindow()
            return
        editor = EpisodeEditor(self.service, episode, zone=self.zone, now=self.now(), parent=self)
        self.editor = editor
        editor.accepted.connect(self.refresh)
        editor.finished.connect(self._editor_finished)
        editor.finished.connect(editor.deleteLater)
        editor.open()

    def _editor_finished(self):
        self.editor = None
        self.log_button.setFocus()
