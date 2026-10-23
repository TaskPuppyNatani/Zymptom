"""Deletion from the existing editor through the real service and database."""

from dataclasses import replace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QMessageBox, QPushButton

from zymptom.domain import EpisodeOutcome
from zymptom.ui.time import to_local

CHILD_TABLES = (
    "episode_functional_demands", "episode_symptoms", "episode_adaptations",
    "episode_consequences", "episode_recovery_actions",
)


def confirm_delete(editor):
    editor.delete_button.click()
    confirmation = editor.delete_confirmation
    assert confirmation is not None and confirmation.isVisible()
    delete = next(button for button in confirmation.buttons()
                  if confirmation.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole)
    delete.click()


def ids(entries):
    return [entries.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(entries.count())
            if entries.item(i).data(Qt.ItemDataRole.UserRole) is not None]


def test_delete_is_only_available_for_existing_episodes(make_editor, service, full_episode):
    new = make_editor()
    assert new.delete_button is None
    assert all(button.text() != "Delete entry" for button in new.findChildren(QPushButton))
    new.delete_entry()
    assert new.delete_confirmation is None
    assert service.list() == []
    new.close()

    service.repository.create(full_episode)
    editor = make_editor(full_episode)
    assert editor.delete_button.isVisible()
    assert editor.delete_button.accessibleName() == "Delete entry"
    assert not editor.delete_button.autoDefault()
    assert editor.delete_button.geometry().right() < editor.cancel_button.geometry().left()
    assert editor.delete_button.geometry().right() < editor.save_button.geometry().left()


@pytest.mark.parametrize("cancel", ["button", "escape", "enter", "window_close", "editor_close"])
def test_confirmation_defaults_to_cancel_and_leaves_data_and_draft_untouched(
    make_editor, service, full_episode, cancel,
):
    full_episode.activity = "Saved <b>activity</b> & details"
    service.repository.create(full_episode)
    editor = make_editor(full_episode)
    editor.activity.setText("Unsaved different activity")
    editor.when.setDateTime(editor.when.dateTime().addDays(1))
    editor.notes.setPlainText("Unsaved notes")
    editor.delete_button.click()
    confirmation = editor.delete_confirmation
    assert confirmation.textFormat() == Qt.TextFormat.PlainText
    assert full_episode.activity in confirmation.informativeText()
    assert to_local(full_episode.occurred_at, editor.zone).toString('ddd, MMM d, yyyy  h:mm AP t') in confirmation.informativeText()
    assert "Unsaved different activity" not in confirmation.informativeText()
    assert confirmation.defaultButton() is confirmation.button(QMessageBox.StandardButton.Cancel)
    assert confirmation.escapeButton() is confirmation.button(QMessageBox.StandardButton.Cancel)
    assert service.get(full_episode.id) == full_episode  # No deletion on opening.
    editor.save()  # Confirmation also blocks a pending editor shortcut.
    assert service.get(full_episode.id) == full_episode
    if cancel == "button":
        confirmation.button(QMessageBox.StandardButton.Cancel).click()
    elif cancel == "escape":
        QTest.keyClick(confirmation, Qt.Key.Key_Escape)
    elif cancel == "enter":
        QTest.keyClick(confirmation, Qt.Key.Key_Return)
    elif cancel == "window_close":
        confirmation.close()
    else:
        editor.close()
    assert editor.delete_confirmation is None
    assert service.get(full_episode.id) == full_episode
    assert editor.activity.text() == "Unsaved different activity"
    assert editor.notes.toPlainText() == "Unsaved notes"
    assert editor.isVisible() == (cancel != "editor_close")


def test_confirmed_delete_cascades_and_does_not_touch_another_episode(
    make_editor, service, connection, full_episode, monkeypatch,
):
    other = replace(full_episode, id=uuid4(), activity="Keep this episode")
    for episode in (full_episode, other):
        service.repository.create(episode)
    editor = make_editor(full_episode)
    editor.activity.setText("Unsaved invalid draft")
    editor.duration.setText("invalid")  # Deletion does not validate/save the draft.
    deleted = QSignalSpy(editor.deleted)
    saved = QSignalSpy(editor.accepted)
    actual = service.delete
    calls = []

    def observed_delete(episode_id):
        calls.append(episode_id)
        return actual(episode_id)

    monkeypatch.setattr(service, "delete", observed_delete)
    confirm_delete(editor)
    assert calls == [full_episode.id]
    assert deleted.count() == 1 and saved.count() == 0
    assert not editor.isVisible()
    assert editor.saved_episode is None
    assert service.get(full_episode.id) is None
    assert service.get(other.id) == other
    assert service.list() == [other]
    for table in CHILD_TABLES:
        assert connection.execute(f"SELECT count(*) FROM {table} WHERE episode_id = ?", (str(full_episode.id),)).fetchone()[0] == 0
        assert connection.execute(f"SELECT count(*) FROM {table} WHERE episode_id = ?", (str(other.id),)).fetchone()[0] > 0


@pytest.mark.parametrize("origin", ["today", "history"])
def test_deletion_refreshes_both_views_even_when_one_is_inactive(window, service, full_episode, origin):
    full_episode.occurred_at = window.now()
    service.repository.create(full_episode)
    other = service.record(activity="Keep", outcome=EpisodeOutcome.COMPLETED_NORMALLY, occurred_at=window.now())
    window.show_history()
    window.show_today()
    if origin == "history":
        window.show_history()
    assert str(full_episode.id) in ids(window.entries)
    assert str(full_episode.id) in ids(window.history.entries)
    window.open_episode_id(full_episode.id)
    confirm_delete(window.editor)
    assert window.editor is None
    assert ids(window.entries) == [str(other.id)]
    assert ids(window.history.entries) == [str(other.id)]


def test_history_keeps_all_active_filters_when_last_matching_episode_is_deleted(window, service, full_episode):
    full_episode.occurred_at = window.now()
    full_episode.functional_demands = ["unique demand"]
    service.repository.create(full_episode)
    other = service.record(activity="Another attempt", outcome=EpisodeOutcome.COMPLETED_NORMALLY, occurred_at=window.now())
    window.show_history()
    history = window.history
    history.search_text.setText("Homework")
    history.outcome.setCurrentIndex(history.outcome.findData(full_episode.outcome))
    history.demand.setCurrentIndex(history.demand.findData("unique demand"))
    local_day = to_local(full_episode.occurred_at, window.zone).date()
    history.start_date.setDate(local_day.addDays(-1))
    history.end_date.setDate(local_day.addDays(1))
    history.start_enabled.setChecked(True)
    history.end_enabled.setChecked(True)
    window.open_episode_id(full_episode.id)
    confirm_delete(window.editor)
    assert ids(history.entries) == []
    assert history.empty_label.isVisible()
    assert history.search_text.text() == "Homework"
    assert history.outcome.currentData() == full_episode.outcome
    assert history.demand.currentData() == "unique demand"
    assert "no longer recorded" in history.demand.currentText()
    assert history.start_enabled.isChecked() and history.end_enabled.isChecked()
    assert history.start_date.date() == local_day.addDays(-1)
    assert history.end_date.date() == local_day.addDays(1)
    assert history.start_date.isEnabled() and history.end_date.isEnabled()
    assert ids(window.entries) == [str(other.id)]
    assert service.get(other.id) == other


@pytest.mark.parametrize("retry", [False, True])
def test_cascade_failure_keeps_editor_data_and_views_and_allows_retry_or_cancel(
    window, service, connection, full_episode, retry,
):
    full_episode.occurred_at = window.now()
    service.repository.create(full_episode)
    window.refresh()
    window.show_history()
    before_history, before_today = ids(window.history.entries), ids(window.entries)
    window.open_episode_id(full_episode.id)
    editor = window.editor
    editor.activity.setText("Keep draft")
    editor.more_button.setChecked(True)
    editor.notes.setPlainText("Draft notes")
    connection.execute("""
        CREATE TRIGGER reject_child_delete BEFORE DELETE ON episode_recovery_actions
        BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END
    """)
    confirm_delete(editor)
    assert window.editor is editor and editor.isVisible()
    assert "Could not delete" in editor.error.text()
    assert editor.error.isVisible() and editor.error.accessibleName() == "Delete error"
    assert editor.activity.text() == "Keep draft"
    assert editor.notes.toPlainText() == "Draft notes"
    assert editor.details.isVisible()
    assert service.get(full_episode.id) == full_episode
    assert not connection.in_transaction
    assert ids(window.history.entries) == before_history
    assert ids(window.entries) == before_today
    if retry:
        connection.execute("DROP TRIGGER reject_child_delete")
        confirm_delete(editor)
        assert window.editor is None
        assert service.get(full_episode.id) is None
    else:
        editor.cancel_button.click()
        assert window.editor is None
        assert service.get(full_episode.id) == full_episode


@pytest.mark.parametrize("failure", [OSError("disk error"), RuntimeError("active transaction"), False])
def test_service_failure_never_reports_success(make_editor, service, full_episode, monkeypatch, failure):
    service.repository.create(full_episode)
    editor = make_editor(full_episode)
    deleted = QSignalSpy(editor.deleted)

    def fail(episode_id):
        if failure is False:
            return False
        raise failure

    monkeypatch.setattr(service, "delete", fail)
    confirm_delete(editor)
    assert deleted.count() == 0
    assert editor.isVisible() and editor.error.isVisible()
    assert service.get(full_episode.id) == full_episode
