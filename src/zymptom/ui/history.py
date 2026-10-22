"""History presentation with repository-backed, composable filters."""

import sqlite3

from PySide6.QtCore import QTimeZone, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from zymptom.services import EpisodeService

from .labels import FUNCTIONAL_DEMANDS, OUTCOMES
from .time import local_date_bounds, to_local
from .widgets import comfortable


class HistoryView(QWidget):
    episode_activated = Signal(str)

    def __init__(self, service: EpisodeService, *, zone: QTimeZone, now, parent=None):
        super().__init__(parent)
        self.service = service
        self.zone = zone
        self.now = now
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.search_text = QLineEdit()
        self.search_text.setPlaceholderText("Search activities")
        self.search_text.setClearButtonEnabled(True)
        self._row(form, "Activity", self.search_text)
        self.outcome = QComboBox()
        self.outcome.addItem("Any outcome", None)
        for value, text in OUTCOMES.items():
            self.outcome.addItem(text, value)
        self._row(form, "Outcome", self.outcome)
        self.demand = QComboBox()
        self.demand.addItem("Any functional demand", None)
        self._row(form, "Functional demand", self.demand)
        layout.addLayout(form)

        dates = QHBoxLayout()
        self.start_enabled = QCheckBox("From")
        comfortable(self.start_enabled, "Filter from start date")
        self.start_date = self._date_control("History start date")
        self.end_enabled = QCheckBox("Through")
        comfortable(self.end_enabled, "Filter through end date")
        self.end_date = self._date_control("History end date, inclusive")
        self._default_dates()
        for widget in (self.start_enabled, self.start_date, self.end_enabled, self.end_date):
            dates.addWidget(widget)
        layout.addLayout(dates)
        self.reset_button = QPushButton("Clear filters")
        comfortable(self.reset_button, "Clear all history filters")
        layout.addWidget(self.reset_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.empty_label = QLabel("No matching episodes. Try changing or clearing the filters.")
        self.empty_label.setProperty("tone", "secondary")
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)
        self.entries = QListWidget()
        self.entries.setObjectName("episodes")
        self.entries.setWordWrap(True)
        self.entries.setSpacing(6)
        self.entries.setAccessibleName("History episodes, newest first, grouped by local date")
        layout.addWidget(self.entries, 1)
        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setAccessibleName("History error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.retry_button = QPushButton("Retry loading History")
        comfortable(self.retry_button, "Retry loading History")
        self.retry_button.hide()
        layout.addWidget(self.retry_button)

        self.search_text.textChanged.connect(self.refresh)
        self.outcome.currentIndexChanged.connect(self.refresh)
        self.demand.currentIndexChanged.connect(self.refresh)
        self.start_enabled.toggled.connect(self.start_date.setEnabled)
        self.end_enabled.toggled.connect(self.end_date.setEnabled)
        self.start_enabled.toggled.connect(self.refresh)
        self.end_enabled.toggled.connect(self.refresh)
        self.start_date.dateChanged.connect(self.refresh)
        self.end_date.dateChanged.connect(self.refresh)
        self.reset_button.clicked.connect(self.reset)
        self.retry_button.clicked.connect(self.refresh)
        self.entries.itemClicked.connect(self._activate)
        self.entries.itemActivated.connect(self._activate)

    @staticmethod
    def _row(form, label, widget):
        comfortable(widget, f"History {label.lower()}")
        text = QLabel(label)
        text.setBuddy(widget)
        form.addRow(text, widget)

    @staticmethod
    def _date_control(name):
        control = QDateEdit()
        control.setCalendarPopup(True)
        control.setDisplayFormat("MMM d, yyyy")
        comfortable(control, name)
        control.setEnabled(False)
        return control

    def _default_dates(self):
        today = to_local(self.now(), self.zone).date()
        self.start_date.setDate(today.addDays(-29))
        self.end_date.setDate(today)

    def reset(self):
        controls = (self.search_text, self.outcome, self.demand, self.start_enabled,
                    self.end_enabled, self.start_date, self.end_date)
        for control in controls:
            control.blockSignals(True)
        try:
            self.search_text.clear()
            self.outcome.setCurrentIndex(0)
            self.demand.setCurrentIndex(0)
            self.start_enabled.setChecked(False)
            self.end_enabled.setChecked(False)
            self.start_date.setEnabled(False)
            self.end_date.setEnabled(False)
            self._default_dates()
        finally:
            for control in controls:
                control.blockSignals(False)
        self.refresh()

    def _refresh_demands(self):
        selected = self.demand.currentData()
        values = self.service.functional_demands()
        self.demand.blockSignals(True)
        try:
            self.demand.clear()
            self.demand.addItem("Any functional demand", None)
            for value in values:
                self.demand.addItem(FUNCTIONAL_DEMANDS.get(value, value), value)
                self.demand.setItemData(self.demand.count() - 1, value, Qt.ItemDataRole.ToolTipRole)
            # Keep an active filter after editing away its final matching tag.
            if selected is not None and selected not in values:
                self.demand.addItem(f"{FUNCTIONAL_DEMANDS.get(selected, selected)} (no longer recorded)", selected)
            self.demand.setCurrentIndex(max(0, self.demand.findData(selected)))
        finally:
            self.demand.blockSignals(False)

    def refresh(self):
        try:
            start, end = local_date_bounds(
                self.start_date.date() if self.start_enabled.isChecked() else None,
                self.end_date.date() if self.end_enabled.isChecked() else None,
                self.zone,
            )
        except (ValueError, OverflowError) as exc:
            self.entries.clear()
            self.empty_label.hide()
            self.show_error(str(exc), retry=False)
            return
        try:
            self._refresh_demands()
            episodes = self.service.search(
                activity=self.search_text.text(), outcome=self.outcome.currentData(),
                functional_demand=self.demand.currentData(),
                occurred_from=start, occurred_before=end,
            )
        except (sqlite3.Error, OSError, ValueError):
            self.empty_label.hide()
            self.show_error("Could not load History. Previously loaded entries may be out of date. Please retry.")
            return
        selected = self.entries.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.entries.clear()
        previous_date = None
        for episode in episodes:
            local = to_local(episode.occurred_at, self.zone)
            if local.date() != previous_date:
                header = QListWidgetItem(local.date().toString("MMMM d, yyyy"))
                header.setFlags(Qt.ItemFlag.ItemIsEnabled)
                font = header.font()
                font.setBold(True)
                header.setFont(font)
                self.entries.addItem(header)
                previous_date = local.date()
            details = []
            if episode.duration_minutes is not None:
                details.append(f"{episode.duration_minutes} min")
            details.append(OUTCOMES[episode.outcome])
            details.extend(FUNCTIONAL_DEMANDS.get(value, value) for value in episode.functional_demands)
            if episode.recovery_minutes is not None:
                details.append(f"Recovery {episode.recovery_minutes} min")
            item = QListWidgetItem(f"{local.toString('h:mm AP')} — {episode.activity}\n{' · '.join(details)}")
            item.setData(Qt.ItemDataRole.UserRole, str(episode.id))
            self.entries.addItem(item)
            if str(episode.id) == selected_id:
                self.entries.setCurrentItem(item)
        self.empty_label.setVisible(not episodes)
        self.error.hide()
        self.retry_button.hide()

    def _activate(self, item):
        episode_id = item.data(Qt.ItemDataRole.UserRole)
        if episode_id is not None:
            self.episode_activated.emit(episode_id)

    def show_error(self, message, *, retry=True):
        self.error.setText(message)
        self.error.show()
        self.retry_button.setVisible(retry)
