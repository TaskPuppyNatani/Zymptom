import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QDate, QTimer, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu, QMessageBox

from zymptom.domain import EpisodeOutcome
from zymptom.ui.editor import EpisodeEditor
from zymptom.ui.time import to_local


@pytest.fixture
def history_records(service):
    records = []
    for activity, instant, outcome, demands in (
        ("Homework reading", datetime(2026, 10, 20, 23, tzinfo=UTC), EpisodeOutcome.STOPPED_EARLY, ["sitting", "custom: pacing"]),
        ("HOMEWORK writing", datetime(2026, 10, 21, 21, 10, tzinfo=UTC), EpisodeOutcome.COMPLETED_WITH_DIFFICULTY, ["sitting", "concentration"]),
        ("Grocery shopping", datetime(2026, 10, 22, 6, 30, tzinfo=UTC), EpisodeOutcome.UNABLE_TO_COMPLETE, ["walking"]),
        ("Rest", datetime(2026, 10, 21, 6, 59, tzinfo=UTC), EpisodeOutcome.STOPPED_EARLY, ["lying_down"]),
    ):
        records.append(service.record(
            activity=activity, occurred_at=instant, outcome=outcome,
            functional_demands=demands, duration_minutes=26, notes="Homework keyword in notes only",
        ))
    return records


def open_history(window, qapp):
    window.history_button.click()
    qapp.processEvents()
    assert window.pages.currentWidget() is window.history
    return window.history


def result_items(history):
    return [history.entries.item(index) for index in range(history.entries.count())
            if history.entries.item(index).data(Qt.ItemDataRole.UserRole) is not None]


def result_ids(history):
    return visible_ids(history.entries)


def visible_ids(entries):
    return [entries.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(entries.count())
            if entries.item(index).data(Qt.ItemDataRole.UserRole) is not None]


def select(combo, value):
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def open_result(window, qapp, index=0):
    history = window.history
    item = result_items(history)[index]
    rect = history.entries.visualItemRect(item)
    QTest.mouseClick(history.entries.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qapp.processEvents()
    assert isinstance(window.editor, EpisodeEditor)
    return window.editor


def choose_context_action(history, item, qapp, label):
    result = {}

    def choose():
        try:
            menu = qapp.activePopupWidget()
            assert isinstance(menu, QMenu)
            result["labels"] = [action.text() for action in menu.actions()]
            action = next(action for action in menu.actions() if action.text() == label)
            action.trigger()
        except Exception as exc:
            result["error"] = exc

    QTimer.singleShot(0, choose)
    rect = history.entries.visualItemRect(item)
    viewport = history.entries.viewport()
    point = rect.center()
    QTest.mouseClick(viewport, Qt.MouseButton.RightButton, pos=point)
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point), Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(viewport, event)
    qapp.processEvents()
    if "error" in result:
        raise result["error"]
    assert result["labels"] == ["Edit", "Delete…"]


def test_history_reachable_loads_newest_first_and_groups_by_local_day(window, qapp, history_records):
    history = open_history(window, qapp)
    assert window.windowTitle() == "Zymptom — History"
    assert not window.day_label.isVisible()
    assert result_ids(history) == [str(history_records[index].id) for index in (2, 1, 3, 0)]
    headers = [history.entries.item(index) for index in range(history.entries.count())
               if history.entries.item(index).data(Qt.ItemDataRole.UserRole) is None]
    assert [item.text() for item in headers] == ["October 21, 2026", "October 20, 2026"]
    assert all(not item.flags() & Qt.ItemFlag.ItemIsSelectable for item in headers)
    assert all(item.foreground().color().name() == "#c4b5fd" for item in headers)
    assert all(item.font().bold() for item in headers)
    assert all(item.data(Qt.ItemDataRole.ForegroundRole) is None for item in result_items(history))
    assert "11:30 PM — Grocery shopping" in result_items(history)[0].text()
    assert "Walking" in result_items(history)[0].text()
    assert "Stopped early" in result_items(history)[-1].text()


def test_activity_search_updates_live_and_blank_restores_results(window, qapp, history_records):
    history = open_history(window, qapp)
    history.search_text.setText("HoMeWo")
    assert result_ids(history) == [str(history_records[index].id) for index in (1, 0)]
    history.search_text.setText("no matching activity")
    assert result_ids(history) == []
    assert history.empty_label.isVisible()
    history.search_text.setText(" \t ")
    assert len(result_ids(history)) == 4


def test_outcome_options_and_filter(window, qapp, history_records):
    history = open_history(window, qapp)
    assert {history.outcome.itemData(index) for index in range(history.outcome.count())} == {None, *EpisodeOutcome}
    select(history.outcome, EpisodeOutcome.UNABLE_TO_COMPLETE)
    assert result_ids(history) == [str(history_records[2].id)]
    select(history.outcome, None)
    assert len(result_ids(history)) == 4


def test_demand_options_come_from_persisted_values_and_filter_exactly(window, qapp, history_records):
    history = open_history(window, qapp)
    values = {history.demand.itemData(index) for index in range(history.demand.count())}
    assert values == {None, "sitting", "concentration", "walking", "lying_down", "custom: pacing"}
    select(history.demand, "custom: pacing")
    assert result_ids(history) == [str(history_records[0].id)]
    select(history.demand, "sitting")
    assert result_ids(history) == [str(history_records[index].id) for index in (1, 0)]


def test_inclusive_local_date_range_includes_entries_on_the_next_utc_day(window, qapp, history_records):
    history = open_history(window, qapp)
    history.start_date.setDate(QDate(2026, 10, 21))
    history.end_date.setDate(QDate(2026, 10, 21))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    assert history.start_date.isEnabled() and history.end_date.isEnabled()
    assert result_ids(history) == [str(history_records[index].id) for index in (2, 1)]
    history.start_enabled.setChecked(False)
    assert len(result_ids(history)) == 4  # Only the inclusive end remains.
    history.end_enabled.setChecked(False)
    assert len(result_ids(history)) == 4


def test_filters_compose_and_clear_restores_all_results(window, qapp, history_records):
    history = open_history(window, qapp)
    history.search_text.setText("homework")
    select(history.outcome, EpisodeOutcome.STOPPED_EARLY)
    select(history.demand, "sitting")
    history.start_date.setDate(QDate(2026, 10, 20))
    history.end_date.setDate(QDate(2026, 10, 20))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    assert result_ids(history) == [str(history_records[0].id)]
    history.reset_button.click()
    assert len(result_ids(history)) == 4
    assert history.search_text.text() == ""
    assert history.outcome.currentData() is None
    assert history.demand.currentData() is None
    assert not history.start_enabled.isChecked() and not history.end_enabled.isChecked()
    assert not history.start_date.isEnabled() and not history.end_date.isEnabled()


def test_invalid_range_is_clear_and_reset_recovers(window, qapp, history_records):
    history = open_history(window, qapp)
    history.start_date.setDate(QDate(2026, 10, 22))
    history.end_date.setDate(QDate(2026, 10, 21))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    assert result_ids(history) == []
    assert "Start date must be on or before end date" in history.error.text()
    assert history.error.isVisible()
    history.reset_button.click()
    assert not history.error.isVisible()
    assert len(result_ids(history)) == 4


def test_history_uses_existing_editor_and_edits_refresh_both_views(window, qapp, service, history_records):
    history = open_history(window, qapp)
    history.search_text.setText("writing")
    editor = open_result(window, qapp)
    original = service.get(history_records[1].id)
    assert editor.original == original
    editor.activity.setText("Homework writing revised")
    editor.save_button.click()
    qapp.processEvents()
    assert window.editor is None
    assert result_ids(history) == [str(original.id)]
    assert "writing revised" in result_items(history)[0].text()
    loaded = service.get(original.id)
    assert loaded.created_at == original.created_at
    assert loaded.updated_at > original.updated_at
    assert loaded.functional_demands == original.functional_demands
    window.today_button.click()
    assert window.windowTitle() == "Zymptom — Today"
    assert window.day_label.isVisible()
    assert any("writing revised" in window.entries.item(index).text() for index in range(window.entries.count()))
    window.history_button.click()
    assert history.search_text.text() == "writing"


def test_edit_no_longer_matching_search_disappears_immediately(window, qapp, history_records):
    history = open_history(window, qapp)
    history.search_text.setText("shopping")
    editor = open_result(window, qapp)
    editor.activity.setText("Cooking")
    editor.save_button.click()
    assert result_ids(history) == []
    assert history.empty_label.isVisible()
    assert history.search_text.text() == "shopping"


def test_edited_date_moves_groups_and_respects_range(window, qapp, history_records):
    history = open_history(window, qapp)
    history.start_date.setDate(QDate(2026, 10, 21))
    history.end_date.setDate(QDate(2026, 10, 21))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    editor = open_result(window, qapp)
    editor.when.setDateTime(to_local(datetime(2026, 10, 20, 18, tzinfo=UTC), window.zone))
    editor.save_button.click()
    assert result_ids(history) == [str(history_records[1].id)]
    history.reset_button.click()
    assert result_ids(history)[0] == str(history_records[1].id)


def test_removing_last_matching_demand_keeps_active_filter_and_new_tags_become_available(window, qapp, history_records):
    history = open_history(window, qapp)
    select(history.demand, "custom: pacing")
    editor = open_result(window, qapp)
    editor.tags["functional_demands"].set_selected("custom: pacing", False)
    editor.tags["functional_demands"].set_selected("new custom demand", True)
    editor.save_button.click()
    assert result_ids(history) == []
    assert history.demand.currentData() == "custom: pacing"
    assert "no longer recorded" in history.demand.currentText()
    assert history.demand.findData("new custom demand") >= 0
    history.reset_button.click()
    assert history.demand.findData("custom: pacing") == -1
    select(history.demand, "new custom demand")
    assert result_ids(history) == [str(history_records[0].id)]


def test_history_cancel_does_not_change_persisted_episode(window, qapp, service, history_records):
    history = open_history(window, qapp)
    original = service.get(history_records[2].id)
    editor = open_result(window, qapp)
    editor.activity.setText("Discard this")
    editor.cancel_button.click()
    assert service.get(original.id) == original
    assert "Grocery shopping" in result_items(history)[0].text()


def test_history_keyboard_activation_skips_date_headers(window, qapp, history_records):
    history = open_history(window, qapp)
    header = history.entries.item(0)
    history.entries.itemClicked.emit(header)
    assert window.editor is None
    history.entries.setCurrentItem(result_items(history)[0])
    history.entries.setFocus()
    QTest.keyClick(history.entries, Qt.Key.Key_Return)
    qapp.processEvents()
    assert window.editor is not None


def test_history_context_menu_edits_the_episode_under_the_cursor(window, qapp, history_records):
    history = open_history(window, qapp)
    target_id = str(history_records[1].id)
    item = next(item for item in result_items(history) if item.data(Qt.ItemDataRole.UserRole) == target_id)

    choose_context_action(history, item, qapp, "Edit")

    assert isinstance(window.editor, EpisodeEditor)
    assert window.editor.original.id == history_records[1].id
    window.editor.cancel_button.click()


def test_history_context_delete_reuses_confirmation_and_cancel_is_non_destructive(
    window, qapp, service, history_records,
):
    history = open_history(window, qapp)
    target_id = str(history_records[0].id)
    item = next(item for item in result_items(history) if item.data(Qt.ItemDataRole.UserRole) == target_id)

    choose_context_action(history, item, qapp, "Delete…")

    editor = window.editor
    assert isinstance(editor, EpisodeEditor)
    assert editor.original.id == history_records[0].id
    confirmation = editor.delete_confirmation
    assert confirmation is not None and confirmation.isVisible()
    assert history_records[0].activity in confirmation.informativeText()
    assert "This cannot be undone" in confirmation.informativeText()
    confirmation.button(QMessageBox.StandardButton.Cancel).click()
    qapp.processEvents()
    assert window.editor is editor and editor.isVisible()
    assert service.get(history_records[0].id) == history_records[0]
    assert str(history_records[0].id) in result_ids(history)
    editor.cancel_button.click()


def test_history_context_delete_refreshes_both_views_and_preserves_filters(
    window, qapp, service, history_records, monkeypatch,
):
    target = history_records[1]
    survivor = service.record(
        activity="Homework survivor", outcome=target.outcome, occurred_at=window.now(),
        functional_demands=["sitting"],
    )
    window.refresh()
    history = open_history(window, qapp)
    history.search_text.setText("Homework")
    select(history.outcome, target.outcome)
    select(history.demand, "sitting")
    local_day = to_local(target.occurred_at, window.zone).date()
    history.start_date.setDate(local_day)
    history.end_date.setDate(local_day)
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    assert set(result_ids(history)) == {str(target.id), str(survivor.id)}
    before_today_ids = set(visible_ids(window.entries))
    assert {str(target.id), str(survivor.id)} <= before_today_ids
    before_filters = (
        history.search_text.text(), history.outcome.currentData(), history.demand.currentData(),
        history.start_enabled.isChecked(), history.end_enabled.isChecked(),
        history.start_date.date(), history.end_date.date(),
    )
    calls = []
    actual_delete = service.delete

    def observed_delete(episode_id):
        calls.append(episode_id)
        return actual_delete(episode_id)

    monkeypatch.setattr(service, "delete", observed_delete)
    item = next(item for item in result_items(history) if item.data(Qt.ItemDataRole.UserRole) == str(target.id))
    choose_context_action(history, item, qapp, "Delete…")
    confirmation = window.editor.delete_confirmation
    delete_button = next(
        button for button in confirmation.buttons()
        if confirmation.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole
    )
    delete_button.click()
    qapp.processEvents()

    assert calls == [target.id]
    assert service.get(target.id) is None
    assert service.get(survivor.id) == survivor
    assert result_ids(history) == [str(survivor.id)]
    assert set(visible_ids(window.entries)) == before_today_ids - {str(target.id)}
    assert str(survivor.id) in visible_ids(window.entries)
    assert (
        history.search_text.text(), history.outcome.currentData(), history.demand.currentData(),
        history.start_enabled.isChecked(), history.end_enabled.isChecked(),
        history.start_date.date(), history.end_date.date(),
    ) == before_filters
    assert window.editor is None


def test_history_empty_space_context_click_has_no_episode_actions(window, qapp, history_records):
    history = open_history(window, qapp)
    viewport = history.entries.viewport()
    blank = viewport.rect().bottomRight()
    assert history.entries.itemAt(blank) is None

    QTest.mouseClick(viewport, Qt.MouseButton.RightButton, pos=blank)
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, blank, viewport.mapToGlobal(blank), Qt.KeyboardModifier.NoModifier,
    )
    qapp.sendEvent(viewport, event)
    qapp.processEvents()

    assert window.editor is None
    assert qapp.activePopupWidget() is None


def test_history_query_failure_retains_results_and_allows_retry(window, qapp, service, history_records, monkeypatch):
    history = open_history(window, qapp)
    before = result_ids(history)
    actual = service.search
    def fail(**kwargs):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(service, "search", fail)
    history.search_text.setText("homework")
    assert history.error.isVisible()
    assert result_ids(history) == before
    monkeypatch.setattr(service, "search", actual)
    history.retry_button.click()
    assert result_ids(history) == [str(history_records[index].id) for index in (1, 0)]
    assert not history.error.isVisible()


def test_dst_fall_day_includes_both_repeated_hours_and_exact_local_boundaries(window, qapp, service):
    start = datetime(2026, 11, 1, 7, tzinfo=UTC)
    end = datetime(2026, 11, 2, 8, tzinfo=UTC)
    records = []
    for index, instant in enumerate((
        start - timedelta(microseconds=1), start,
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC), datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
        end - timedelta(microseconds=1), end,
    )):
        records.append(service.record(activity=f"Attempt {index}", outcome=EpisodeOutcome.STOPPED_EARLY, occurred_at=instant))
    history = open_history(window, qapp)
    history.start_date.setDate(QDate(2026, 11, 1))
    history.end_date.setDate(QDate(2026, 11, 1))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    assert result_ids(history) == [str(records[index].id) for index in (4, 3, 2, 1)]
    assert "1:30 AM — Attempt 3" in result_items(history)[1].text()
    assert "1:30 AM — Attempt 2" in result_items(history)[2].text()
    assert history.entries.item(0).text() == "November 1, 2026"


def test_logging_from_history_refreshes_today_without_resetting_history_filters(window, qapp, service, history_records):
    history = open_history(window, qapp)
    history.search_text.setText("shopping")
    window.log_button.click()
    qapp.processEvents()
    editor = window.editor
    editor.activity.setText("Fresh Homework")
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.STOPPED_EARLY))
    editor.save_button.click()
    assert result_ids(history) == [str(history_records[2].id)]
    assert history.search_text.text() == "shopping"
    window.today_button.click()
    assert any("Fresh Homework" in window.entries.item(index).text() for index in range(window.entries.count()))
