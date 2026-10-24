"""Small accessible controls used by the episode editor."""

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)


def comfortable(widget: QWidget, name: str) -> None:
    widget.setAccessibleName(name)
    widget.setMinimumHeight(widget.fontMetrics().height() * 2 + 8)
    if isinstance(widget, QPushButton):
        widget.setAutoDefault(False)
        widget.setDefault(False)


class MultiSelectComboBox(QComboBox):
    """Check several suggestions without closing the popup after each choice."""

    def __init__(self, suggestions: dict[str, str], parent=None):
        super().__init__(parent)
        self._checked_values = []
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItem("", None)
        for value, label in suggestions.items():
            self.addItem(label, value)
            item = self.model().item(self.count() - 1)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
        self.lineEdit().setPlaceholderText("Choose one or more values")
        self.setAccessibleDescription(
            "Check one or more choices with a click or Space. Close the list, then choose Add."
        )
        self.setToolTip(self.accessibleDescription())
        self.model().dataChanged.connect(self._checks_changed)
        self.view().setRowHidden(0, True)
        self.view().viewport().installEventFilter(self)
        self.view().installEventFilter(self)
        self.lineEdit().textEdited.connect(self._typed_text)

    def checked_values(self) -> list[str]:
        return self._checked_values.copy()

    def reset_choices(self) -> None:
        with QSignalBlocker(self.model()):
            for row in range(1, self.count()):
                self.model().item(row).setCheckState(Qt.CheckState.Unchecked)
        self._checked_values.clear()
        self.setCurrentIndex(0)
        self.setEditText("")

    def _typed_text(self, text: str) -> None:
        # Retain the existing typed-value path without treating a checked-value
        # summary as a new tag when the user switches to typing.
        if self._checked_values:
            self.reset_choices()
            self.setEditText(text)

    def _checks_changed(self, *args) -> None:
        checked = [
            self.itemData(row) for row in range(1, self.count())
            if self.model().item(row).checkState() == Qt.CheckState.Checked
        ]
        self._checked_values = [value for value in self._checked_values if value in checked]
        self._checked_values.extend(value for value in checked if value not in self._checked_values)
        self._show_checked_values()

    def _show_checked_values(self) -> None:
        self.setEditText("; ".join(self.itemText(self.findData(value)) for value in self._checked_values))

    def _toggle(self, index) -> None:
        if index.isValid() and self.itemData(index.row()) is not None:
            item = self.model().itemFromIndex(index)
            state = Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
            item.setCheckState(state)

    def eventFilter(self, watched, event):
        if watched is self.view().viewport() and event.type() in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
        ) and event.button() == Qt.MouseButton.LeftButton:
            index = self.view().indexAt(event.position().toPoint())
            self.view().setCurrentIndex(index)
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._toggle(index)
            return True
        if watched in (self.view(), self.view().viewport()) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._toggle(self.view().currentIndex())
                return True
        return super().eventFilter(watched, event)

    def hidePopup(self) -> None:
        super().hidePopup()
        if self._checked_values:
            self._show_checked_values()


class TagEditor(QWidget):
    """Add unique ordered tags, retaining legacy values faithfully until save."""

    changed = Signal()

    def __init__(self, name: str, suggestions: dict[str, str], values: list[str], parent=None):
        super().__init__(parent)
        self.suggestions = suggestions
        self.setAccessibleName(name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.input = MultiSelectComboBox(suggestions)
        comfortable(self.input, name)
        self.add_button = QPushButton("Add")
        comfortable(self.add_button, f"Add {name.lower()}")
        row.addWidget(self.input, 1)
        row.addWidget(self.add_button)
        layout.addLayout(row)
        self.items = QListWidget()
        self.items.setAccessibleName(f"Selected {name.lower()}")
        self.items.setMinimumHeight(self.fontMetrics().height() * 3 + 12)
        self.items.setMaximumHeight(self.fontMetrics().height() * 5 + 16)
        layout.addWidget(self.items)
        self.remove_button = QPushButton("Remove selected")
        comfortable(self.remove_button, f"Remove selected {name.lower()}")
        layout.addWidget(self.remove_button)
        for value in values:
            self._append(value)
        self._update_visibility()
        self.add_button.clicked.connect(self.add_current)
        self.remove_button.clicked.connect(self.remove_current)

    def values(self) -> list[str]:
        return [self.items.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.items.count())]

    def _append(self, value: str) -> None:
        item = QListWidgetItem(self.suggestions.get(value, value))
        item.setData(Qt.ItemDataRole.UserRole, value)
        item.setToolTip(value)
        self.items.addItem(item)

    def add_current(self) -> None:
        values = self.input.checked_values()
        if not values:
            text = self.input.currentText().strip()
            if not text:
                return
            data = self.input.currentData()
            values = [data if data and self.suggestions.get(data) == text else text]
        current = set(self.values())
        added = False
        for value in values:
            if value not in current:
                self._append(value)
                current.add(value)
                added = True
        self.input.reset_choices()
        self._update_visibility()
        if added:
            self.changed.emit()

    def remove_current(self) -> None:
        index = self.items.currentRow()
        if index >= 0:
            self.items.takeItem(index)
            self._update_visibility()
            self.changed.emit()

    def set_selected(self, value: str, selected: bool) -> None:
        current = self.values()
        if selected and value not in current:
            self._append(value)
        elif not selected:
            for index in reversed(range(self.items.count())):
                if self.items.item(index).data(Qt.ItemDataRole.UserRole) == value:
                    self.items.takeItem(index)
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self) -> None:
        has_values = self.items.count() > 0
        self.items.setVisible(has_values)
        self.remove_button.setVisible(has_values)
