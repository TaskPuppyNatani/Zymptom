from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from zymptom.domain import EpisodeOutcome


def test_new_entry_focus_and_logical_first_tab_steps(make_editor, qapp):
    editor = make_editor()
    assert QApplication.focusWidget() is editor.activity
    QTest.keyClick(editor.activity, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is editor.when
    # Qt traverses the date/time's editable sections before leaving the control.
    for _ in range(editor.when.sectionCount() + 1):
        QTest.keyClick(editor.when, Qt.Key.Key_Tab)
        if QApplication.focusWidget() is editor.duration:
            break
    assert QApplication.focusWidget() is editor.duration
    QTest.keyClick(editor.duration, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is editor.outcome
    assert editor.save_button.accessibleName() == "Save"
    assert editor.activity.accessibleName() == "What happened?"
    assert editor.more_button.accessibleDescription().startswith("Collapsed")


def test_enter_in_a_field_never_saves_even_with_a_complete_form(make_editor, service):
    editor = make_editor()
    editor.activity.setText("Homework")
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.STOPPED_EARLY))
    for field in (editor.activity, editor.duration, editor.recovery_minutes):
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_Return)
        assert editor.isVisible()
        assert service.list() == []
    editor.save_button.setFocus()
    QTest.keyClick(editor.save_button, Qt.Key.Key_Space)
    assert editor.result() == QDialog.DialogCode.Accepted
    assert len(service.list()) == 1


def test_escape_discards_new_entry(make_editor, service):
    editor = make_editor()
    editor.activity.setText("Unsaved")
    QTest.keyClick(editor.activity, Qt.Key.Key_Escape)
    assert editor.result() == QDialog.DialogCode.Rejected
    assert not editor.isVisible()
    assert service.list() == []


def test_primary_actions_have_explicit_keyboard_shortcuts(window, qapp, service):
    window.log_button.setFocus()
    QTest.keyClick(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert window.editor is not None
    editor = window.editor
    editor.activity.setText("Keyboard entry")
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.COMPLETED_NORMALLY))
    QTest.keyClick(editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert len(service.list()) == 1
