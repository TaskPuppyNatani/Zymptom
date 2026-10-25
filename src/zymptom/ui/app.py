"""Own the QApplication and database connection for the desktop session."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from zymptom.services.journal import JournalSession
from zymptom.persistence.backups import BackupError

from .window import MainWindow
from .branding import application_icon
from .theme import apply_theme


def run(database: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("Zymptom")
    app.setApplicationName("Zymptom")
    apply_theme(app)
    app.setWindowIcon(application_icon())
    try:
        if database is None:
            directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
            directory.mkdir(parents=True, exist_ok=True)
            database = directory / "zymptom.sqlite3"
        session = JournalSession(database)
    except BackupError as exc:
        QMessageBox.critical(None, "Required backup failed", f"The journal was not upgraded because its safety backup could not be verified.\n\n{exc}")
        return 1
    except (sqlite3.Error, OSError, RuntimeError):
        QMessageBox.critical(None, "Could not open Zymptom", "The local journal could not be opened. Check the database location and try again.")
        return 1
    with closing(session):
        window = MainWindow(session.episodes, backup_session=session)
        window.show()
        return app.exec()
