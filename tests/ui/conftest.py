import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimeZone
from PySide6.QtWidgets import QApplication

from zymptom.services import EpisodeService
from zymptom.ui.editor import EpisodeEditor
from zymptom.ui.window import MainWindow
from zymptom.ui.theme import apply_theme

NOW = datetime(2026, 10, 21, 21, 10, tzinfo=UTC)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    yield app


@pytest.fixture
def zone():
    return QTimeZone(b"America/Los_Angeles")


@pytest.fixture
def service(repository):
    return EpisodeService(repository)


@pytest.fixture
def window(qapp, service, zone):
    window = MainWindow(service, zone=zone, now=lambda: NOW)
    window.show()
    qapp.processEvents()
    yield window
    if window.editor:
        window.editor.reject()
    window.close()
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def make_editor(qapp, service, zone):
    editors = []

    def make(episode=None, **kwargs):
        editor = EpisodeEditor(service, episode, zone=zone, now=NOW, **kwargs)
        editors.append(editor)
        editor.show()
        qapp.processEvents()
        return editor

    yield make
    for editor in editors:
        editor.close()
    qapp.processEvents()
