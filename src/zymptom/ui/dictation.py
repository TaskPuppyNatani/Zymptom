"""Review-first dictation UI. Text only reaches the editor on explicit acceptance."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from zymptom.dictation.session import DictationSession

from .widgets import comfortable


def append_transcript(existing: str, transcript: str, *, multiline: bool) -> str:
    text = transcript.strip()
    if not multiline:
        text = " ".join(text.split())
    if not text:
        return existing
    separator = ""
    if existing and not existing[-1].isspace():
        separator = "\n" if multiline else " "
    return existing + separator + text


class DictationDialog(QDialog):
    def __init__(self, target_name, parent=None, *, session_factory=DictationSession):
        super().__init__(parent)
        self.setWindowTitle(f"Dictate — {target_name}")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(540, 380)
        self.session = session_factory(self)
        self.inserted_text = ""
        layout = QVBoxLayout(self)
        destination = QLabel(f"Insert into: {target_name}")
        destination.setWordWrap(True)
        layout.addWidget(destination)
        privacy = QLabel("Local dictation • Recordings are temporary • Inserting does not save the entry")
        privacy.setProperty("tone", "secondary")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Dictation status")
        layout.addWidget(self.status)
        self.transcript = QPlainTextEdit()
        self.transcript.setAccessibleName(f"Review transcript for {target_name}")
        self.transcript.setPlaceholderText("Your transcript will appear here for review.")
        self.transcript.setTabChangesFocus(True)
        layout.addWidget(self.transcript, 1)
        row = QHBoxLayout()
        self.start_button = QPushButton("Start again")
        self.stop_button = QPushButton("Stop")
        self.cancel_button = QPushButton("Cancel")
        self.insert_button = QPushButton("Insert text")
        self.insert_button.setProperty("primary", True)
        for button in (self.start_button, self.stop_button, self.cancel_button, self.insert_button):
            comfortable(button, button.text())
            row.addWidget(button)
        layout.addLayout(row)
        self.stop_button.setToolTip("Stop recording and transcribe locally")
        self.cancel_button.setToolTip("Discard this dictation and leave the field unchanged")
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.session.stop)
        self.cancel_button.clicked.connect(self.reject)
        self.insert_button.clicked.connect(self.accept)
        self.transcript.textChanged.connect(self._can_insert)
        self.session.changed.connect(self._state_changed)
        self.session.transcript_ready.connect(self._review)
        self._state_changed("idle", "Ready for local dictation.")

    def start(self):
        self.transcript.clear()
        self.session.start()

    def _state_changed(self, state, message):
        self.status.setText(message)
        self.status.setProperty("role", "error" if state == "error" else "dictation")
        self.status.setProperty("state", state)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.start_button.setEnabled(state in ("idle", "review", "error"))
        self.stop_button.setEnabled(state == "listening")
        self.transcript.setEnabled(state == "review")
        self._can_insert()
        if state == "listening":
            self.stop_button.setFocus()
        elif state == "error":
            self.start_button.setFocus()

    def _can_insert(self):
        self.insert_button.setEnabled(self.session.state == "review" and bool(self.transcript.toPlainText().strip()))

    def _review(self, text):
        self.transcript.setPlainText(text)
        self.transcript.setFocus()

    def accept(self):
        if not self.insert_button.isEnabled():
            return
        self.inserted_text = self.transcript.toPlainText()
        super().accept()

    def done(self, result):
        self.session.cancel()
        self.transcript.clear()
        super().done(result)
