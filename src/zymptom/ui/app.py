"""Own the QApplication and database connection for the desktop session."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.services import EpisodeService

from .window import MainWindow


def run(database: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("Zymptom")
    app.setApplicationName("Zymptom")
    try:
        if database is None:
            directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
            directory.mkdir(parents=True, exist_ok=True)
            database = directory / "zymptom.sqlite3"
        connection = connect_database(database)
    except (sqlite3.Error, OSError, RuntimeError):
        QMessageBox.critical(None, "Could not open Zymptom", "The local journal could not be opened. Check the database location and try again.")
        return 1
    with closing(connection):
        window = MainWindow(EpisodeService(EpisodeRepository(connection)))
        window.show()
        return app.exec()
