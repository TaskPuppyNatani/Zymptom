"""Fail-closed ownership of the dedicated disposable SQLite file."""

import json
import os
import stat
from pathlib import Path


class IsolationError(RuntimeError):
    pass


def data_home(environ=None):
    env = os.environ if environ is None else environ
    configured = Path(env.get("XDG_DATA_HOME", ""))
    return configured if configured.is_absolute() else Path.home() / ".local" / "share"


def sandbox_database(production_path: Path, *, root: Path | None = None) -> Path:
    directory = (root if root is not None else data_home()) / "zymptom" / "sandbox-sync"
    if directory.is_symlink():
        raise IsolationError("The sandbox directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = directory.resolve(strict=True)
    database = directory / "journal.sqlite3"
    marker = directory / ".sandbox-owner.json"
    production = production_path.resolve(strict=False)
    if database.resolve(strict=False) == production or database.is_symlink() or marker.is_symlink():
        raise IsolationError("The sandbox path is not isolated from the normal journal.")
    if database.exists() and production.exists() and os.path.samefile(database, production):
        raise IsolationError("The sandbox database aliases the normal journal.")
    if database.exists() != marker.exists():
        raise IsolationError("Sandbox ownership cannot be established. No journal was opened.")
    if not database.exists():
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(database, flags, 0o600)
        try:
            info = os.fstat(fd)
            marker_fd = os.open(marker, flags, 0o600)
            with os.fdopen(marker_fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "device": info.st_dev, "inode": info.st_ino}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            database.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
    try:
        info = database.stat(follow_symlinks=False)
        owner = json.loads(marker.read_text(encoding="utf-8"))
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino
                or owner != {"version": 1, "device": info.st_dev, "inode": info.st_ino}):
            raise IsolationError("Sandbox ownership changed. No journal was opened.")
    except (OSError, ValueError, TypeError) as exc:
        raise IsolationError("Sandbox ownership cannot be verified. No journal was opened.") from exc
    return database
