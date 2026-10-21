"""Small accessible controls used by the episode editor."""

from PySide6.QtCore import Qt, Signal
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


class TagEditor(QWidget):
    """Append/remove ordered tags without rewriting custom or duplicate values."""

    changed = Signal()

    def __init__(self, name: str, suggestions: dict[str, str], values: list[str], parent=None):
        super().__init__(parent)
        self.suggestions = suggestions
        self.setAccessibleName(name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.input = QComboBox()
        self.input.setEditable(True)
        self.input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.input.addItem("", None)
        for value, label in suggestions.items():
            self.input.addItem(label, value)
        self.input.lineEdit().setPlaceholderText("Choose or type a value")
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
        text = self.input.currentText().strip()
        if not text:
            return
        data = self.input.currentData()
        value = data if data and self.suggestions.get(data) == text else text
        self._append(value)
        self.input.setCurrentIndex(0)
        self._update_visibility()
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
