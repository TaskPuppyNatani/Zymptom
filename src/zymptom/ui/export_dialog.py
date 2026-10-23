"""Desktop orchestration for local export and summary generation."""

import sqlite3
from pathlib import Path

from PySide6.QtCore import QDate, QTimeZone
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
)

from zymptom.exports import (
    ExportRange, render_csv, render_json, render_markdown, render_summary_details,
    summarize, write_atomically,
)
from zymptom.services import EpisodeService
from zymptom.services.exports import EpisodeExportService

from .branding import brand_mark
from .time import local_date_bounds, to_local
from .widgets import comfortable

_FORMATS = {
    "json": ("JSON", "json"),
    "csv": ("CSV", "csv"),
    "markdown": ("Markdown / text", "md"),
}


class ExportDialog(QDialog):
    def __init__(self, service: EpisodeService, *, zone: QTimeZone, now, parent=None):
        super().__init__(parent)
        self.export_service = EpisodeExportService(service)
        self.zone = zone
        self.now = now
        self._episodes = []
        self._summary = None
        self._export_range = None
        self.setWindowTitle("Export / Summary")
        self.setAccessibleName("Export functional episodes and view summary")
        self.resize(700, 700)

        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Choose an optional local date range. Leave both dates unchecked to include all recorded episodes. "
            "Summaries report recorded values only."
        )
        introduction.setProperty("tone", "secondary")
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        dates = QFormLayout()
        dates.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        local_today = to_local(now, zone).date()
        self.start_enabled = QCheckBox("From")
        self.start_date = self._date_control("Export start date", local_today)
        self.end_enabled = QCheckBox("Through")
        self.end_date = self._date_control("Export end date", local_today)
        from_row = QHBoxLayout()
        from_row.addWidget(self.start_enabled)
        from_row.addWidget(self.start_date, 1)
        through_row = QHBoxLayout()
        through_row.addWidget(self.end_enabled)
        through_row.addWidget(self.end_date, 1)
        dates.addRow("Start date", from_row)
        dates.addRow("End date", through_row)
        layout.addLayout(dates)

        self.format = QComboBox()
        self.format.setObjectName("exportFormat")
        for value, (label, _) in _FORMATS.items():
            self.format.addItem(label, value)
        dates.addRow("Format", self.format)

        preview_label = QLabel("Summary preview")
        preview_label.setProperty("tone", "secondary")
        layout.addWidget(preview_label)
        self.preview = QTextBrowser()
        self.preview.setAccessibleName("Deterministic summary preview")
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        layout.addWidget(self.preview, 1)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setAccessibleName("Export error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.export_button = QPushButton("Export…")
        self.export_button.setProperty("primary", True)
        comfortable(self.cancel_button, "Cancel export")
        comfortable(self.export_button, "Export selected episodes")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.export_button)
        layout.addLayout(actions)
        layout.addWidget(brand_mark(self))

        self.start_date.setEnabled(False)
        self.end_date.setEnabled(False)
        self.start_enabled.toggled.connect(self.start_date.setEnabled)
        self.end_enabled.toggled.connect(self.end_date.setEnabled)
        self.start_enabled.toggled.connect(self._refresh_preview)
        self.end_enabled.toggled.connect(self._refresh_preview)
        self.start_date.dateChanged.connect(self._refresh_preview)
        self.end_date.dateChanged.connect(self._refresh_preview)
        self.format.currentIndexChanged.connect(self._refresh_preview)
        self.cancel_button.clicked.connect(self.reject)
        self.export_button.clicked.connect(self.export_selected)
        self._refresh_preview()

    @staticmethod
    def _date_control(name, value: QDate):
        control = QDateEdit()
        control.setCalendarPopup(True)
        control.setDisplayFormat("MMM d, yyyy")
        control.setDate(value)
        comfortable(control, name)
        return control

    @staticmethod
    def _date(value: QDate):
        return value.toPython()

    def _prepare(self):
        start_date = self._date(self.start_date.date()) if self.start_enabled.isChecked() else None
        end_date = self._date(self.end_date.date()) if self.end_enabled.isChecked() else None
        start, end = local_date_bounds(
            self.start_date.date() if start_date else None,
            self.end_date.date() if end_date else None,
            self.zone,
        )
        timezone_name = bytes(self.zone.id()).decode("utf-8")
        export_range = ExportRange(start_date, end_date, timezone_name)
        episodes = self.export_service.select(occurred_from=start, occurred_before=end)
        return export_range, episodes, summarize(episodes)

    def _refresh_preview(self, *_):
        try:
            export_range, episodes, summary = self._prepare()
        except (sqlite3.Error, OSError, ValueError, KeyError):
            self._episodes = []
            self._summary = None
            self._export_range = None
            self.preview.clear()
            self.error.setText(
                "Could not prepare the selected date range. Check the dates and local journal, then try again."
            )
            self.error.show()
            self.export_button.setEnabled(False)
            return
        self._episodes = episodes
        self._summary = summary
        self._export_range = export_range
        self.preview.setMarkdown(render_summary_details(summary, export_range))
        self.error.hide()
        self.export_button.setEnabled(True)

    def _choose_save_path(self, format_name: str) -> Path | None:
        _, suffix = _FORMATS[format_name]
        dialog = QFileDialog(self, "Export functional episodes")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, False)
        dialog.setNameFilter({
            "json": "JSON files (*.json)",
            "csv": "CSV files (*.csv)",
            "markdown": "Markdown / text files (*.md *.markdown *.txt)",
        }[format_name])
        dialog.setDefaultSuffix(suffix)
        dialog.selectFile(f"zymptom-export.{suffix}")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected = dialog.selectedFiles()
        return Path(selected[0]) if selected else None

    def export_selected(self):
        if self._summary is None or self._export_range is None:
            return
        format_name = self.format.currentData()
        path = self._choose_save_path(format_name)
        if path is None:
            return
        try:
            if format_name == "json":
                contents = render_json(self._episodes, self._export_range)
            elif format_name == "csv":
                contents = render_csv(self._episodes)
            else:
                contents = render_markdown(self._episodes, self._summary, self._export_range)
            write_atomically(path, contents)
        except (OSError, ValueError):
            self.error.setText(
                "Could not write the export. Any existing file was left unchanged; choose another location or retry."
            )
            self.error.show()
            return
        self.accept()
