"""Shared episode actions for list-based desktop views."""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidget, QMenu


def connect_episode_context_menu(
    entries: QListWidget,
    *,
    on_edit: Callable[[str], None],
    on_delete: Callable[[str], None],
) -> None:
    """Add Edit/Delete actions that always use the episode under the cursor."""
    entries.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show_context_menu(position):
        item = entries.itemAt(position)
        episode_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if episode_id is None:
            return

        entries.setCurrentItem(item)
        menu = QMenu(entries)
        edit_action = menu.addAction("Edit")
        delete_action = menu.addAction("Delete…")
        edit_action.triggered.connect(
            lambda checked=False, target_id=episode_id: on_edit(target_id)
        )
        delete_action.triggered.connect(
            lambda checked=False, target_id=episode_id: on_delete(target_id)
        )
        try:
            menu.exec(entries.mapToGlobal(position))
        finally:
            menu.deleteLater()

    entries.customContextMenuRequested.connect(show_context_menu)
