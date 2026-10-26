"""Validated, standalone SQLite snapshots; no live-database filesystem copies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

KINDS = {"daily", "manual", "migration", "pre-restore", "safety"}
AUTOMATIC_KINDS = {"daily", "migration"}
NAME = re.compile(r"^(\d{8}T\d{12}Z)-(daily|manual|migration|pre-restore|safety)-([0-9a-f]{16})-([0-9a-f]{32})\.sqlite3$")


class BackupError(RuntimeError):
    """A backup or restore could not be completed safely."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: datetime
    kind: str
    size: int
    schema_version: int | None
    checksum: str | None
    content_hash: str | None
    valid: bool
    error: str = ""


def readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)


def copy_sqlite(source: sqlite3.Connection, destination: sqlite3.Connection, *, timeout: float = 10) -> None:
    """SQLite holds an atomic destination transaction, including on cancellation."""
    if source.in_transaction or destination.in_transaction:
        raise BackupError("Finish the active database transaction before backing up or restoring.")
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if status != sqlite3.SQLITE_DONE and time.monotonic() > deadline:
            raise BackupError("The journal is busy. Close other database tools and try again.")

    source.backup(destination, pages=256, progress=progress, sleep=0.05)


def content_hash(connection: sqlite3.Connection) -> str:
    """Hash a consistent logical snapshot, without storing any journal text."""
    if connection.in_transaction:
        raise BackupError("Finish the active database transaction before creating a backup.")
    connection.execute("BEGIN")
    try:
        return transaction_content_hash(connection)
    finally:
        connection.rollback()


def transaction_content_hash(connection: sqlite3.Connection) -> str:
    """Reuse the same fingerprint while a merge owns a stable transaction."""
    if not connection.in_transaction:
        raise BackupError("A consistent database transaction is required for this fingerprint.")
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def file_checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_connection(connection: sqlite3.Connection) -> tuple[int, str]:
    from .migrations import MIGRATIONS
    from .repository import EpisodeRepository, SCALAR_FIELDS, TAG_TABLES

    try:
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise BackupError("The snapshot failed SQLite integrity validation.")
        history = [tuple(row) for row in connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )]
        connection.execute("SELECT applied_at FROM schema_migrations LIMIT 0")
        if history and history[-1][0] > MIGRATIONS[-1].version:
            raise BackupError("This backup uses a newer schema and cannot be restored by this version of Zymptom.")
        expected = [(migration.version, migration.name) for migration in MIGRATIONS[:len(history)]]
        if not history or history != expected:
            raise BackupError("The backup's migration history is not supported by this version of Zymptom.")
        connection.execute(f"SELECT {', '.join(SCALAR_FIELDS)} FROM episodes LIMIT 0")
        for table in TAG_TABLES.values():
            connection.execute(f"SELECT episode_id, position, tag FROM {table} LIMIT 0")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("The snapshot contains inconsistent child records.")
        connection.row_factory = sqlite3.Row
        EpisodeRepository(connection).list()
        if history[-1][0] >= 2:
            from zymptom.sync.storage import validate_state
            validate_state(connection)
        return history[-1][0], content_hash(connection)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise BackupError("The snapshot is unreadable or is not a valid Zymptom journal.") from exc


def validate_snapshot(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise BackupError("The snapshot is missing or is not a regular backup file.")
    try:
        with closing(readonly(path)) as connection:
            return validate_connection(connection)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError("The snapshot is unreadable or is not a valid Zymptom journal.") from exc


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BackupStore:
    def __init__(self, database: Path, *, directory: Path | None = None, retain: int = 30):
        self.database = Path(database).resolve()
        self.directory = Path(directory).resolve() if directory else self.database.parent / "backups"
        self.source_id = hashlib.sha256(os.fsencode(self.database)).hexdigest()[:16]
        self.retain = max(1, retain)
        self.warning = ""

    def temporary_path(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".zymptom-", suffix=".tmp", dir=self.directory)
        os.close(descriptor)
        return Path(name)

    def inspect(self, path: Path) -> BackupInfo:
        path = Path(path).absolute()
        match = NAME.fullmatch(path.name)
        if path.parent != self.directory or match is None or match[3] != self.source_id:
            raise BackupError("Choose a backup belonging to this journal from the backup list.")
        try:
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            return BackupInfo(path, datetime.min.replace(tzinfo=UTC), match[2], 0, None, None, None, False, "The backup filename has an invalid timestamp.")
        size = 0
        try:
            if path.is_symlink():
                raise BackupError("Symbolic links are not trusted backup files.")
            size = path.stat().st_size
            checksum = file_checksum(path)
            try:
                metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                metadata = {}
            if isinstance(metadata, dict) and metadata.get("sha256") not in (None, checksum):
                raise BackupError("The backup checksum has changed; this snapshot cannot be trusted.")
            version, logical_hash = validate_snapshot(path)
            return BackupInfo(path, created, match[2], size, version, checksum, logical_hash, True)
        except (BackupError, OSError) as exc:
            return BackupInfo(path, created, match[2], size, None, None, None, False, str(exc))

    def list(self) -> list[BackupInfo]:
        if not self.directory.exists():
            return []
        paths = [path for path in self.directory.glob("*.sqlite3")
                 if (match := NAME.fullmatch(path.name)) and match[3] == self.source_id]
        return sorted((self.inspect(path) for path in paths), key=lambda item: (item.created_at, item.path.name), reverse=True)

    def _metadata(self, info: BackupInfo) -> None:
        temporary = self.temporary_path()
        try:
            payload = {
                "filename": info.path.name, "created_at": info.created_at.isoformat(),
                "kind": info.kind, "schema_version": info.schema_version,
                "size": info.size, "validation": "ok", "sha256": info.checksum,
                "content_hash": info.content_hash,
            }
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, info.path.with_suffix(".json"))
            sync_directory(self.directory)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, connection: sqlite3.Connection, kind: str = "manual", *, now: datetime | None = None) -> BackupInfo:
        if kind not in KINDS:
            raise ValueError("Unknown backup kind")
        now = now or datetime.now().astimezone()
        if now.utcoffset() is None:
            raise ValueError("Backup time must have a timezone")
        source = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
        if not source or Path(source).resolve() != self.database:
            raise BackupError("The backup connection does not belong to this journal.")
        self.warning = ""
        temporary = None
        try:
            temporary = self.temporary_path()
            with closing(sqlite3.connect(temporary, isolation_level=None)) as destination:
                copy_sqlite(connection, destination)
                # Publish a standalone snapshot with no WAL/shm dependencies.
                destination.execute("PRAGMA journal_mode = DELETE")
            version, logical_hash = validate_snapshot(temporary)
            checksum = file_checksum(temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            final = self.directory / f"{stamp}-{kind}-{self.source_id}-{uuid4().hex}.sqlite3"
            os.replace(temporary, final)
            sync_directory(self.directory)
            info = BackupInfo(final, now.astimezone(UTC), kind, final.stat().st_size, version, checksum, logical_hash, True)
            try:
                self._metadata(info)
            except OSError:
                self.warning = "The snapshot is verified, but its metadata file could not be saved. It can be reconstructed from the snapshot."
            self.prune()
            return info
        except (sqlite3.Error, OSError) as exc:
            raise BackupError("Could not create a verified local backup. Check free space and folder permissions, then retry.") from exc
        finally:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                self.warning = "A temporary backup file could not be removed. It is not included in the verified backup history."

    def daily_if_changed(self, connection: sqlite3.Connection, *, now: datetime) -> BackupInfo | None:
        daily = next((info for info in self.list() if info.valid and info.kind == "daily"), None)
        if daily is not None:
            if daily.created_at.astimezone(now.tzinfo).date() >= now.date():
                return None
            if daily.content_hash == content_hash(connection):
                return None
        return self.create(connection, "daily", now=now)

    def prune(self) -> None:
        try:
            automatic = [info for info in self.list() if info.valid and info.kind in AUTOMATIC_KINDS]
            for info in automatic[self.retain:]:
                try:
                    info.path.unlink()
                    info.path.with_suffix(".json").unlink(missing_ok=True)
                except OSError:
                    self.warning = "The new backup is verified, but some older automatic backups could not be removed."
        except (OSError, BackupError):
            self.warning = "The new backup is verified, but automatic retention could not be completed."
