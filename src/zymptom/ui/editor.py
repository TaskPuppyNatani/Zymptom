"""Fast entry with optional detail, edited independently of persisted records."""

import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from PySide6.QtCore import QTimeZone, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from zymptom.domain import FunctionalEpisode, ValidationError
from zymptom.persistence import EpisodeNotFoundError
from zymptom.services import EpisodeService

from . import labels
from .time import to_local, to_utc
from .widgets import TagEditor, comfortable


class EpisodeEditor(QDialog):
    def __init__(
        self, service: EpisodeService, episode: FunctionalEpisode | None = None,
        *, zone: QTimeZone | None = None, now=None, parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.original = deepcopy(episode)
        self.saved_episode = None
        self.zone = zone or QTimeZone.systemTimeZone()
        self.setWindowTitle("Edit entry" if episode else "Log something")
        self.resize(660, 720)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        intro = QLabel("Record an attempt, even if you stopped or couldn't start.")
        intro.setWordWrap(True)
        body.addWidget(intro)
        common = QFormLayout()
        common.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        common.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        body.addLayout(common)

        self.activity = QLineEdit(episode.activity if episode else "")
        self.activity.setPlaceholderText("For example, homework or making lunch")
        self._row(common, "What happened?", self.activity)
        self.when = QDateTimeEdit()
        self.when.setTimeZone(self.zone)
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("ddd, MMM d, yyyy  h:mm AP")
        self.when.setDateTime(to_local(episode.occurred_at if episode else (now or datetime.now(UTC)), self.zone))
        self._initial_when = self.when.dateTime()
        self._row(common, "When?", self.when)
        self.duration = self._minutes(episode.duration_minutes if episode else None)
        self._row(common, "How long? (minutes)", self.duration)
        self.outcome = self._combo(labels.OUTCOMES, episode.outcome if episode else None, optional=False)
        if episode is None:
            self.outcome.setCurrentIndex(-1)
            self.outcome.setPlaceholderText("Choose an outcome")
        self._row(common, "Outcome", self.outcome)

        body.addWidget(QLabel("Quick adaptations / needs"))
        quick = QGridLayout()
        body.addLayout(quick)
        self.quick_buttons = {}
        quick_options = [*(('adaptations', value, text) for value, text in labels.ADAPTATIONS.items()),
                         *(('recovery_actions', value, text) for value, text in labels.RECOVERY_ACTIONS.items())]
        for index, (field, value, text) in enumerate(quick_options):
            button = QCheckBox(text)
            comfortable(button, text)
            quick.addWidget(button, index // 2, index % 2)
            self.quick_buttons[value] = (button, field)
        recovery_form = QFormLayout()
        recovery_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.recovery_minutes = self._minutes(episode.recovery_minutes if episode else None)
        self._row(recovery_form, "Recovery (minutes)", self.recovery_minutes)
        body.addLayout(recovery_form)

        self.more_button = QPushButton("More details ▸")
        self.more_button.setCheckable(True)
        comfortable(self.more_button, "More details")
        body.addWidget(self.more_button)
        self.details = QWidget()
        detail_form = QFormLayout(self.details)
        detail_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        detail_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.tags = {}
        for field, title, suggestions in (
            ("functional_demands", "Functional demands", labels.FUNCTIONAL_DEMANDS),
            ("symptoms", "Symptoms / difficulties", labels.SYMPTOMS),
            ("adaptations", "Adaptations / needs", labels.ADAPTATIONS),
            ("consequences", "Consequences", labels.CONSEQUENCES),
            ("recovery_actions", "Recovery actions", labels.RECOVERY_ACTIONS),
        ):
            editor = TagEditor(title, suggestions, getattr(episode, field) if episode else [])
            self.tags[field] = editor
            self._row(detail_form, title, editor)
        self.symptom_notes = self._notes(episode.symptom_notes if episode else None)
        self._row(detail_form, "Symptom notes", self.symptom_notes)
        self.help_level = self._combo(labels.HELP_LEVELS, episode.help_level if episode else None)
        self._row(detail_form, "Help", self.help_level)
        self.help_notes = self._notes(episode.help_notes if episode else None)
        self._row(detail_form, "Help notes", self.help_notes)
        self.recovery_required = self._combo({True: "Yes", False: "No"}, episode.recovery_required if episode else None)
        self._row(detail_form, "Recovery required?", self.recovery_required)
        self.resume_ability = self._combo(labels.RESUME_ABILITIES, episode.resume_ability if episode else None)
        self._row(detail_form, "Able to resume", self.resume_ability)
        self.notes = self._notes(episode.notes if episode else None)
        self._row(detail_form, "Notes", self.notes)
        self._initial_notes = {
            field: getattr(self, field).toPlainText()
            for field in ("symptom_notes", "help_notes", "notes")
        }
        body.addWidget(self.details)
        self.details.hide()
        self.more_button.setAccessibleDescription("Collapsed. Activate to show optional fields.")
        self.more_button.toggled.connect(self._toggle_details)
        for value, (button, field) in self.quick_buttons.items():
            button.toggled.connect(lambda checked, value=value, field=field: self.tags[field].set_selected(value, checked))
        for field in ("adaptations", "recovery_actions"):
            self.tags[field].changed.connect(self._sync_quick_buttons)
        self._sync_quick_buttons()
        body.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setAccessibleName("Save error")
        self.error.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.error.hide()
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("&Cancel")
        self.save_button = QPushButton("&Save")
        comfortable(self.cancel_button, "Cancel")
        comfortable(self.save_button, "Save")
        self.save_button.setToolTip("Save this entry (Ctrl+S)")
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.save)
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self.save)
        # Tab order follows the form, then detail controls, then Cancel and Save.
        self.setTabOrder(self.activity, self.when)
        self.setTabOrder(self.when, self.duration)
        self.setTabOrder(self.duration, self.outcome)

    @staticmethod
    def _row(form, label, widget):
        comfortable(widget, label)
        text = QLabel(label)
        text.setBuddy(widget.input if isinstance(widget, TagEditor) else widget)
        form.addRow(text, widget)

    @staticmethod
    def _minutes(value):
        widget = QLineEdit("" if value is None else str(value))
        widget.setPlaceholderText("Optional")
        widget.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        return widget

    @staticmethod
    def _notes(value):
        widget = QPlainTextEdit(value or "")
        widget.setMinimumHeight(widget.fontMetrics().height() * 3 + 12)
        widget.setMaximumHeight(widget.fontMetrics().height() * 5 + 16)
        widget.setTabChangesFocus(True)
        return widget

    @staticmethod
    def _combo(choices, value, optional=True):
        widget = QComboBox()
        if optional:
            widget.addItem("Not recorded", None)
        for data, label in choices.items():
            widget.addItem(label, data)
        index = widget.findData(value)
        widget.setCurrentIndex(max(index, 0))
        return widget

    def _sync_quick_buttons(self):
        for value, (button, field) in self.quick_buttons.items():
            button.blockSignals(True)
            button.setChecked(value in self.tags[field].values())
            button.blockSignals(False)

    def _toggle_details(self, expanded):
        self.details.setVisible(expanded)
        self.more_button.setText("More details ▾" if expanded else "More details ▸")
        self.more_button.setAccessibleDescription("Expanded" if expanded else "Collapsed")

    def showEvent(self, event):
        super().showEvent(event)
        if self.original is None:
            QTimer.singleShot(0, self.activity.setFocus)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Enter in a field never becomes an implicit form submission.
            focused = self.focusWidget()
            if isinstance(focused, QPushButton) and focused.isEnabled():
                focused.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def _read_minutes(self, widget, name):
        text = widget.text().strip()
        if not text:
            return None
        try:
            minutes = int(text)
        except ValueError:
            raise ValidationError(f"{name}: enter a whole number of minutes, or leave it blank.") from None
        if minutes < 0 or minutes > 9223372036854775807:
            raise ValidationError(f"{name}: enter a non-negative number of minutes within the supported range.")
        return minutes

    def _read_notes(self, field):
        text = getattr(self, field).toPlainText()
        previous = getattr(self.original, field) if self.original else None
        # Preserve the distinction between an unchanged empty string and None.
        return previous if text == self._initial_notes[field] else (text or None)

    def save(self):
        self.error.hide()
        try:
            if not self.activity.text().strip():
                self.activity.setFocus()
                raise ValidationError("Please enter what you tried to do in ‘What happened?’.")
            if self.outcome.currentIndex() < 0:
                self.outcome.setFocus()
                raise ValidationError("Please choose an outcome.")
            occurred_at = (
                self.original.occurred_at
                if self.original and self.when.dateTime() == self._initial_when
                else to_utc(self.when.dateTime())
            )
            fields = dict(
                activity=self.activity.text(), outcome=self.outcome.currentData(),
                occurred_at=occurred_at,
                duration_minutes=self._read_minutes(self.duration, "How long"),
                recovery_minutes=self._read_minutes(self.recovery_minutes, "Recovery"),
                help_level=self.help_level.currentData(),
                recovery_required=self.recovery_required.currentData(),
                resume_ability=self.resume_ability.currentData(),
                **{field: editor.values() for field, editor in self.tags.items()},
                **{field: self._read_notes(field) for field in ("symptom_notes", "help_notes", "notes")},
            )
            if self.original is None:
                saved = self.service.record(**fields)
            else:
                saved = self.service.update(replace(self.original, **fields))
        except (ValidationError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        except (sqlite3.Error, OSError, OverflowError, EpisodeNotFoundError):
            self.error.setText("Could not save this entry. Your changes are still here. Please try Save again, or Cancel to discard them.")
            self.error.show()
            self.error.setFocus()
            return
        self.saved_episode = saved
        self.accept()
