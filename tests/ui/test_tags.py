from copy import deepcopy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from zymptom.domain import EpisodeOutcome


FIELDS = ("functional_demands", "symptoms", "adaptations", "consequences", "recovery_actions")


def check_values(control, values):
    for value in values:
        row = control.input.findData(value)
        assert row >= 0
        control.input.model().item(row).setCheckState(Qt.CheckState.Checked)


@pytest.mark.parametrize("field", FIELDS)
def test_popup_selects_multiple_in_selection_order_and_one_add_saves_all(make_editor, qapp, service, field):
    editor = make_editor()
    editor.more_button.setChecked(True)
    control = editor.tags[field]
    first, second, third = list(control.suggestions)[:3]
    expected = [third, first, second]
    combo = control.input
    combo.showPopup()
    qapp.processEvents()
    for value in expected:
        index = combo.model().index(combo.findData(value), 0)
        combo.view().scrollTo(index)
        qapp.processEvents()
        rect = combo.view().visualRect(index)
        QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert combo.view().isVisible()
    assert combo.checked_values() == expected
    index = combo.model().index(combo.findData(first), 0)
    combo.view().scrollTo(index)
    qapp.processEvents()
    point = combo.view().visualRect(index).center()
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert combo.checked_values() == [third, second]
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=point)
    expected = [third, second, first]
    assert combo.checked_values() == expected
    assert control.values() == []
    assert control.input.accessibleName()
    assert "Space" in control.input.accessibleDescription()
    combo.hidePopup()
    control.add_button.click()

    assert control.values() == expected
    assert combo.checked_values() == []
    assert combo.currentText() == ""
    assert all(combo.model().item(row).checkState() == Qt.CheckState.Unchecked
               for row in range(1, combo.count()))
    editor.activity.setText("Multi-value entry")
    editor.outcome.setCurrentIndex(editor.outcome.findData(EpisodeOutcome.COMPLETED_NORMALLY))
    editor.save_button.click()
    assert editor.result() == QDialog.DialogCode.Accepted
    assert getattr(service.list()[0], field) == expected


@pytest.mark.parametrize("field", FIELDS)
def test_repeated_and_mixed_adds_only_new_values_and_remove_readd_appends(make_editor, field):
    control = make_editor().tags[field]
    first, second, third = list(control.suggestions)[:3]
    check_values(control, [third, first])
    control.add_button.click()
    assert control.values() == [third, first]

    check_values(control, [third, first])
    control.add_button.click()
    assert control.values() == [third, first]
    assert control.input.checked_values() == []

    check_values(control, [first, second, third])
    control.add_button.click()
    assert control.values() == [third, first, second]
    assert control.items.count() == 3

    control.items.setCurrentRow(0)
    control.remove_button.click()
    assert control.values() == [first, second]
    check_values(control, [third])
    control.add_button.click()
    assert control.values() == [first, second, third]
    assert control.items.count() == 3


def test_keyboard_can_check_multiple_choices_without_saving(make_editor, qapp, service):
    editor = make_editor()
    editor.more_button.setChecked(True)
    control = editor.tags["symptoms"]
    combo = control.input
    combo.showPopup()
    qapp.processEvents()
    for value in ("fatigue", "pain"):
        combo.view().setCurrentIndex(combo.model().index(combo.findData(value), 0))
        QTest.keyClick(combo.view(), Qt.Key.Key_Space)
        assert combo.view().isVisible()
    assert combo.checked_values() == ["fatigue", "pain"]
    QTest.keyClick(combo.view(), Qt.Key.Key_Escape)
    control.add_button.setFocus()
    QTest.keyClick(control.add_button, Qt.Key.Key_Space)
    assert control.values() == ["fatigue", "pain"]
    assert editor.isVisible()
    assert service.list() == []


def test_quick_choices_share_duplicate_protection_with_multi_add(make_editor):
    editor = make_editor()
    for field in ("adaptations", "recovery_actions"):
        control = editor.tags[field]
        first, second = list(control.suggestions)[:2]
        editor.quick_buttons[first][0].setChecked(True)
        check_values(control, [first, second])
        control.add_button.click()
        assert control.values() == [first, second]
        assert editor.quick_buttons[second][0].isChecked()
        editor.quick_buttons[first][0].setChecked(False)
        check_values(control, [first])
        control.add_button.click()
        assert control.values() == [second, first]


def test_existing_typed_value_path_also_prevents_duplicates(make_editor):
    control = make_editor().tags["symptoms"]
    for _ in range(2):
        control.input.setEditText("existing custom value ☕")
        control.add_button.click()
    assert control.values() == ["existing custom value ☕"]


def test_switching_to_existing_typed_path_does_not_add_the_choice_summary(make_editor):
    control = make_editor().tags["symptoms"]
    check_values(control, ["pain", "fatigue"])
    QTest.keyClick(control.input.lineEdit(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(control.input.lineEdit(), "existing custom value")
    assert control.input.checked_values() == []
    control.add_button.click()
    assert control.values() == ["existing custom value"]


def test_legacy_duplicates_load_and_cancel_without_writes(make_editor, service, connection, legacy_episode):
    changes = connection.total_changes
    before = deepcopy(legacy_episode)
    editor = make_editor(service.get(legacy_episode.id))
    for field in FIELDS:
        assert editor.tags[field].values() == getattr(before, field)
    editor.cancel_button.click()
    assert connection.total_changes == changes
    assert service.get(before.id) == before
    assert legacy_episode == before


def test_saving_legacy_episode_normalizes_all_fields_without_mutating_original(make_editor, service, legacy_episode):
    before = deepcopy(legacy_episode)
    editor = make_editor(service.get(legacy_episode.id))
    editor.activity.setText("Edited legacy entry")
    editor.save_button.click()
    assert editor.result() == QDialog.DialogCode.Accepted
    saved = service.get(before.id)
    expected = {
        "functional_demands": ["sitting", "concentration"],
        "symptoms": ["fatigue", "pain"],
        "adaptations": ["changed_position", "reduced_task"],
        "consequences": ["missed_next_activity", "needed_rest"],
        "recovery_actions": ["lay_down", "quiet_room"],
    }
    for field, values in expected.items():
        assert getattr(saved, field) == values
        setattr(saved, field, getattr(before, field))
    saved.activity = before.activity
    assert saved.updated_at > before.updated_at
    saved.updated_at = before.updated_at
    assert saved == before
    assert editor.original == before
    assert legacy_episode == before
