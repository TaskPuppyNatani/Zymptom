import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, HelpLevel, ResumeAbility


def select(combo, value):
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def add_tag(editor, field, value):
    control = editor.tags[field]
    select(control.input, value)
    control.add_button.click()


def open_first(window, qapp):
    rect = window.entries.visualItemRect(window.entries.item(0))
    QTest.mouseClick(window.entries.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qapp.processEvents()
    assert window.editor is not None
    return window.editor


def test_main_window_initializes(window, repository):
    assert window.windowTitle() == "Zymptom — Today"
    assert window.entries.count() == 0
    assert window.empty_label.isVisible()
    assert repository.list() == []


def test_today_uses_local_calendar_day_in_chronological_order(window, service):
    for activity, instant in (
        ("Tomorrow", datetime(2026, 10, 22, 7, tzinfo=UTC)),
        ("Late today", datetime(2026, 10, 22, 6, 59, tzinfo=UTC)),
        ("Yesterday", datetime(2026, 10, 21, 6, 59, tzinfo=UTC)),
        ("Start of today", datetime(2026, 10, 21, 7, tzinfo=UTC)),
    ):
        service.record(activity=activity, outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=instant)
    window.refresh()
    assert window.entries.count() == 2
    assert "12:00 AM\nStart of today" in window.entries.item(0).text()
    assert "11:59 PM\nLate today" in window.entries.item(1).text()
    assert not window.empty_label.isVisible()


def test_realistic_create_reopen_edit_and_round_trip(window, qapp, service):
    QTest.mouseClick(window.log_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    editor = window.editor
    assert QApplication.focusWidget() is editor.activity
    assert not editor.details.isVisible()
    QTest.keyClicks(editor.activity, "Homework")
    QTest.keyClicks(editor.duration, "32")
    select(editor.outcome, EpisodeOutcome.STOPPED_EARLY)
    editor.quick_buttons["changed_position"][0].click()
    QTest.keyClicks(editor.recovery_minutes, "45")
    editor.save_button.click()
    qapp.processEvents()

    assert window.editor is None
    assert window.entries.count() == 1
    assert window.entries.item(0).text() == "2:10 PM\nHomework\n32 min · Stopped early · Recovery 45 min"
    episode = service.list()[0]
    original_created, original_updated = episode.created_at, episode.updated_at
    assert episode.adaptations == ["changed_position"]
    assert episode.occurred_at == datetime(2026, 10, 21, 21, 10, tzinfo=UTC)

    editor = open_first(window, qapp)
    assert editor.activity.text() == "Homework"
    assert editor.duration.text() == "32"
    assert editor.quick_buttons["changed_position"][0].isChecked()
    editor.more_button.click()
    assert editor.details.isVisible()
    add_tag(editor, "functional_demands", "sitting")
    add_tag(editor, "functional_demands", "concentration")
    select(editor.resume_ability, ResumeAbility.LATER_SAME_DAY)
    editor.save_button.click()
    qapp.processEvents()

    loaded = service.get(episode.id)
    assert loaded.created_at == original_created
    assert loaded.updated_at > original_updated
    assert loaded.functional_demands == ["sitting", "concentration"]
    assert loaded.resume_ability is ResumeAbility.LATER_SAME_DAY
    editor = open_first(window, qapp)
    assert editor.original == loaded
    assert editor.tags["functional_demands"].values() == ["sitting", "concentration"]
    assert editor.resume_ability.currentData() == ResumeAbility.LATER_SAME_DAY
    assert editor.recovery_minutes.text() == "45"
    assert editor.outcome.currentData() == EpisodeOutcome.STOPPED_EARLY


def test_edit_refreshes_today_immediately(window, qapp, service):
    episode = service.record(activity="Original", outcome=EpisodeOutcome.COMPLETED_NORMALLY, occurred_at=window.now())
    window.refresh()
    editor = open_first(window, qapp)
    editor.activity.setText("Changed activity")
    editor.save_button.click()
    assert "Changed activity" in window.entries.item(0).text()
    assert service.get(episode.id).activity == "Changed activity"


def test_complete_existing_episode_preserves_untouched_fields(make_editor, service, full_episode):
    full_episode.notes = ""
    full_episode.symptom_notes = None
    full_episode.help_notes = "Preserve\r\noriginal line endings."
    full_episode.help_level = HelpLevel.NONE
    full_episode.recovery_required = False
    full_episode.adaptations = ["custom 'tag'\nwith newline", "changed_position", "changed_position"]
    service.repository.create(full_episode)
    before = deepcopy(full_episode)
    editor = make_editor(full_episode)
    editor.activity.setText("Only activity changed")
    editor.save_button.click()
    loaded = service.get(full_episode.id)
    assert loaded.activity == "Only activity changed"
    loaded.activity = before.activity
    assert loaded.updated_at > before.updated_at
    loaded.updated_at = before.updated_at
    assert loaded == before
    assert full_episode == before  # The caller's object was never used as a draft.


@pytest.mark.parametrize("existing", [False, True])
def test_cancel_discards_only_the_draft(make_editor, service, full_episode, existing):
    if existing:
        service.repository.create(full_episode)
    editor = make_editor(full_episode if existing else None)
    editor.activity.setText("Unsaved activity")
    editor.tags["symptoms"].set_selected("new symptom", True)
    editor.cancel_button.click()
    assert editor.result() == QDialog.DialogCode.Rejected
    assert service.list() == ([full_episode] if existing else [])


@pytest.mark.parametrize("field,value", [("activity", "  "), ("duration", "-1"), ("recovery_minutes", "-1"), ("duration", "not minutes")])
def test_validation_failure_keeps_draft_and_does_not_persist(make_editor, service, field, value):
    editor = make_editor()
    editor.activity.setText("Homework")
    select(editor.outcome, EpisodeOutcome.STOPPED_EARLY)
    getattr(editor, field).setText(value)
    editor.save_button.click()
    assert editor.isVisible()
    assert editor.error.isVisible()
    assert getattr(editor, field).text() == value
    assert service.list() == []


def test_outcome_requires_an_explicit_choice(make_editor, service):
    editor = make_editor()
    editor.activity.setText("Homework")
    editor.save_button.click()
    assert "choose an outcome" in editor.error.text()
    assert service.list() == []


@pytest.mark.parametrize("existing", [False, True])
def test_save_failure_keeps_contents_and_allows_retry(make_editor, service, full_episode, monkeypatch, existing):
    if existing:
        service.repository.create(full_episode)
    editor = make_editor(full_episode if existing else None)
    editor.activity.setText("Draft survives failure")
    select(editor.outcome, EpisodeOutcome.STOPPED_EARLY)
    editor.duration.setText("32")
    editor.quick_buttons["changed_position"][0].setChecked(True)
    method = "update" if existing else "record"
    real_method = getattr(service, method)

    def fail(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service, method, lambda *args, **kwargs: fail())
    editor.save_button.click()
    assert editor.isVisible()
    assert editor.error.isVisible()
    assert "changes are still here" in editor.error.text()
    assert editor.activity.text() == "Draft survives failure"
    assert editor.duration.text() == "32"
    assert "changed_position" in editor.tags["adaptations"].values()
    assert service.list() == ([full_episode] if existing else [])
    monkeypatch.setattr(service, method, real_method)
    editor.save_button.click()
    assert editor.result() == QDialog.DialogCode.Accepted
    assert len(service.list()) == 1
    assert service.list()[0].activity == "Draft survives failure"


def test_quick_choices_and_details_use_the_same_collections(make_editor, service):
    editor = make_editor()
    editor.activity.setText("Making lunch")
    select(editor.outcome, EpisodeOutcome.COMPLETED_WITH_ADAPTATION)
    for button, field in editor.quick_buttons.values():
        button.setChecked(True)
    editor.tags["adaptations"].set_selected("sat_down", False)
    assert not editor.quick_buttons["sat_down"][0].isChecked()
    editor.save_button.click()
    saved = service.list()[0]
    assert saved.adaptations == ["changed_position", "took_break", "needed_help"]
    assert saved.recovery_actions == ["lay_down", "medication", "sensory_recovery"]


def test_today_reload_failure_retains_visible_entries_and_allows_retry(window, service, monkeypatch):
    service.record(activity="Walk", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=window.now())
    window.refresh()
    real_list = service.list
    monkeypatch.setattr(service, "list", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    window.refresh()
    assert window.entries.count() == 1
    assert window.error.isVisible()
    monkeypatch.setattr(service, "list", real_list)
    window.retry_button.click()
    assert not window.error.isVisible()
    assert window.entries.count() == 1


def test_today_rolls_to_new_local_day_without_date_navigation(window, service):
    service.record(activity="Today", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=window.now())
    window.refresh()
    assert window.entries.count() == 1
    window.now = lambda: datetime(2026, 10, 22, 7, tzinfo=UTC)
    window.day_timer.timeout.emit()
    assert window.entries.count() == 0


def test_application_launches_against_temporary_database(tmp_path):
    script = """
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from zymptom.__main__ import main
from zymptom.ui.window import MainWindow
app = QApplication([])
observed = []
def inspect():
    observed.extend(w for w in app.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible())
    app.quit()
QTimer.singleShot(50, inspect)
assert main(['--database', sys.argv[1]]) == 0
assert len(observed) == 1
assert observed[0].entries.count() == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "app.sqlite3")],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert result.returncode == 0, result.stderr
