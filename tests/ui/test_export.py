import json
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QFileDialog

from zymptom.domain import EpisodeOutcome
from zymptom.ui.export_dialog import ExportDialog


def test_export_surface_is_reachable_and_defaults_to_all_records(window, qapp, service, full_episode):
    service.repository.create(full_episode)

    window.export_button.click()
    qapp.processEvents()

    dialog = window.export_dialog
    assert isinstance(dialog, ExportDialog)
    assert dialog.isVisible()
    assert not dialog.start_enabled.isChecked()
    assert not dialog.end_enabled.isChecked()
    assert "1 functional attempt was recorded" in dialog.preview.toPlainText()
    assert dialog.export_button.isEnabled()
    dialog.reject()
    qapp.processEvents()
    assert window.export_dialog is None


def test_export_ui_selects_inclusive_date_range_and_saves_json(
    window, qapp, service, tmp_path,
):
    included = service.record(
        activity="Café, \"errands\"\nand travel",
        outcome=EpisodeOutcome.COMPLETED_WITH_DIFFICULTY,
        occurred_at=datetime(2026, 10, 21, 6, 59, tzinfo=UTC),
        notes="line one\nline two ☕",
    )
    service.record(
        activity="Outside selected dates",
        outcome=EpisodeOutcome.STOPPED_EARLY,
        occurred_at=datetime(2026, 10, 22, 7, 0, tzinfo=UTC),
    )
    window.export_button.click()
    qapp.processEvents()
    dialog = window.export_dialog
    path = tmp_path / "journal.json"
    dialog.start_enabled.setChecked(True)
    dialog.end_enabled.setChecked(True)
    dialog.start_date.setDate(QDate(2026, 10, 20))
    dialog.end_date.setDate(QDate(2026, 10, 20))
    dialog._choose_save_path = lambda format_name: path
    dialog.export_button.click()
    qapp.processEvents()

    assert window.export_dialog is None
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert exported["date_range"] == {
        "start": "2026-10-20", "end": "2026-10-20", "timezone": "America/Los_Angeles",
    }
    assert [row["id"] for row in exported["episodes"]] == [str(included.id)]
    assert exported["episodes"][0]["activity"] == included.activity


@pytest.mark.parametrize(
    ("format_name", "filename", "header"),
    [
        ("csv", "journal.csv", "id,occurred_at,activity"),
        ("markdown", "journal.md", "# Zymptom Functional Episode Report"),
    ],
)
def test_export_ui_saves_each_other_supported_format(
    window, qapp, service, tmp_path, format_name, filename, header,
):
    service.record(activity="Reading", outcome=EpisodeOutcome.STOPPED_EARLY)
    window.export_button.click()
    qapp.processEvents()
    dialog = window.export_dialog
    dialog.format.setCurrentIndex(dialog.format.findData(format_name))
    path = tmp_path / filename
    dialog._choose_save_path = lambda selected_format: path
    dialog.export_button.click()
    qapp.processEvents()

    assert path.read_text(encoding="utf-8").startswith(header)
    assert window.export_dialog is None


def test_export_failure_keeps_dialog_open_and_surfaces_error(window, qapp, service, tmp_path, monkeypatch):
    service.record(activity="An activity", outcome=EpisodeOutcome.STOPPED_EARLY)
    window.export_button.click()
    qapp.processEvents()
    dialog = window.export_dialog
    dialog._choose_save_path = lambda format_name: tmp_path / "report.json"

    def fail_write(path, contents):
        raise OSError("disk full")

    monkeypatch.setattr("zymptom.ui.export_dialog.write_atomically", fail_write)
    dialog.export_button.click()
    qapp.processEvents()

    assert window.export_dialog is dialog
    assert dialog.isVisible()
    assert dialog.error.isVisible()
    assert "Could not write the export" in dialog.error.text()
    assert dialog.export_button.isEnabled()
    dialog.reject()
    qapp.processEvents()
    assert window.export_dialog is None


def test_save_dialog_keeps_native_overwrite_confirmation_enabled(window, qapp, monkeypatch):
    window.export_button.click()
    qapp.processEvents()
    dialog = window.export_dialog
    inspected = {}

    def reject_and_inspect(save_dialog):
        inspected["confirm_overwrite"] = not save_dialog.testOption(
            QFileDialog.Option.DontConfirmOverwrite,
        )
        return QFileDialog.DialogCode.Rejected

    monkeypatch.setattr(QFileDialog, "exec", reject_and_inspect)
    assert dialog._choose_save_path("json") is None
    assert inspected["confirm_overwrite"] is True
    dialog.reject()
    qapp.processEvents()
