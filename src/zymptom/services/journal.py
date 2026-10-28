"""Own the journal connection and orchestrate recoverable local restores."""

from __future__ import annotations

import sqlite3
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence.backups import (
    BackupError, BackupInfo, BackupStore, content_hash, copy_sqlite, file_checksum,
    readonly, validate_connection, validate_snapshot,
)
from zymptom.persistence.migrations import apply_migrations

from .episodes import EpisodeService
from zymptom.sync.identity import DeviceIdentity, parse_device_id


@dataclass(frozen=True)
class RecoveryPreview:
    path: Path
    schema_version: int
    migration_required: bool
    entry_count: int
    oldest: datetime | None
    newest: datetime | None
    checksum: str
    content_hash: str
    identity: tuple[int, int, int, int]


class RecoveryError(BackupError):
    def __init__(self, message: str, *, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class RestorePlan:
    selected: BackupInfo | RecoveryPreview
    safety: BackupInfo
    staged: Path
    checksum: str
    content_hash: str


class JournalSession:
    def __init__(self, database: Path, *, device_id=None):
        self.database = Path(database).resolve()
        self._device_id = parse_device_id(device_id) if device_id is not None else DeviceIdentity().get()
        self.backups = BackupStore(self.database)
        self.connection = connect_database(self.database)
        self.episodes = EpisodeService(EpisodeRepository(self.connection, device_id=self._device_id))
        self._plan = None

    def close(self) -> None:
        self.cancel_restore()
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def before_destructive_change(self) -> BackupInfo:
        """Future import/merge entry points must call this before changing data."""
        if self.connection is None:
            raise BackupError("The journal is unavailable. Close Zymptom before trying again.")
        return self.backups.create(self.connection, "safety")

    def prepare_restore(self, path: Path) -> RestorePlan:
        if self.connection is None:
            raise BackupError("The journal is unavailable. Close Zymptom before trying again.")
        self.cancel_restore()
        info = self.backups.inspect(path)
        if not info.valid:
            raise BackupError(info.error)
        return self._prepare_snapshot(info, readonly)

    def inspect_recovery(self, path: Path) -> RecoveryPreview:
        """Inspect a closed standalone journal without writing to its files."""
        from zymptom.persistence.migrations import MIGRATIONS
        if self.connection is None:
            raise RecoveryError("The current journal is unavailable.", kind="unavailable")
        try:
            source = Path(path).resolve(strict=True)
            if not source.is_file() or source == self.database or os.path.samefile(source, self.database):
                raise RecoveryError("Choose a different journal file.", kind="current")
            self._require_standalone(source)
            before = source.stat()
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            checksum = file_checksum(source)
            with closing(self._readonly_recovery(source)) as connection:
                version, logical_hash = validate_connection(connection)
                self._validate_recovery_schema(connection, version)
                if version >= 2 and any(row[0].startswith("firebase-sandbox:") for row in connection.execute("SELECT DISTINCT provider FROM sync_acknowledgements")):
                    raise RecoveryError("Sandbox sync state cannot be imported into the normal journal.", kind="sandbox")
                episodes = EpisodeRepository(connection).list()
            self._require_standalone(source)
            after = source.stat()
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or file_checksum(source) != checksum:
                raise RecoveryError("The selected journal changed. Inspect it again.", kind="changed")
            dates = [episode.occurred_at for episode in episodes]
            return RecoveryPreview(source, version, version < MIGRATIONS[-1].version, len(episodes),
                                   min(dates, default=None), max(dates, default=None), checksum, logical_hash, identity)
        except RecoveryError:
            raise
        except BackupError as exc:
            kind = "newer" if "newer schema" in str(exc) else "invalid"
            raise RecoveryError("The selected journal cannot be inspected safely.", kind=kind) from exc
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            raise RecoveryError("The selected journal cannot be inspected safely.") from exc

    @staticmethod
    def _require_standalone(path: Path) -> None:
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RecoveryError("Close the selected journal before inspecting it; SQLite sidecar files are present.", kind="open")

    @staticmethod
    def _readonly_recovery(path: Path) -> sqlite3.Connection:
        # A closed standalone source can be read without creating SQLite sidecars.
        return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None)

    @staticmethod
    def _validate_recovery_schema(connection: sqlite3.Connection, version: int) -> None:
        """Require the schema generated by the supported Zymptom migrations."""
        from zymptom.persistence.migrations import MIGRATIONS
        query = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as expected:
            apply_migrations(expected, tuple(migration for migration in MIGRATIONS if migration.version <= version), before_migrate=lambda _: None)
            if [tuple(row) for row in connection.execute(query)] != [tuple(row) for row in expected.execute(query)]:
                raise RecoveryError("The selected journal schema is not recognized.")

    def prepare_recovery(self, preview: RecoveryPreview) -> RestorePlan:
        if not isinstance(preview, RecoveryPreview):
            raise RecoveryError("Inspect the journal before recovering it.")
        current = self.inspect_recovery(preview.path)
        if current != preview:
            raise RecoveryError("The selected journal changed. Inspect it again.", kind="changed")
        return self._prepare_snapshot(current, self._readonly_recovery)

    def _prepare_snapshot(self, info: BackupInfo | RecoveryPreview, opener) -> RestorePlan:
        if self.connection is None:
            raise BackupError("The journal is unavailable. Close Zymptom before trying again.")
        self.cancel_restore()
        staged = self.backups.temporary_path()
        phase = "stage"
        try:
            if isinstance(info, RecoveryPreview):
                self._require_standalone(info.path)
            with closing(opener(info.path)) as source, closing(sqlite3.connect(staged, isolation_level=None)) as target:
                copy_sqlite(source, target)
                if validate_connection(target)[1] != info.content_hash:
                    raise BackupError("The selected backup changed while it was being staged. Select it again.")
                # The selected snapshot itself already protects this staging
                # migration. The primary journal has not been touched.
                phase = "migration"
                apply_migrations(target, before_migrate=lambda _: None)
                phase = "stage"
            _, logical_hash = validate_snapshot(staged)
            if isinstance(info, RecoveryPreview):
                self._require_standalone(info.path)
            if file_checksum(info.path) != info.checksum:
                raise BackupError("The selected backup changed while it was being prepared. Select it again.")
            phase = "backup"
            safety = self.backups.create(self.connection, "pre-restore")
            plan = RestorePlan(info, safety, staged, file_checksum(staged), logical_hash)
            self._plan = plan
            return plan
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            staged.unlink(missing_ok=True)
            if isinstance(info, RecoveryPreview):
                raise RecoveryError("Recovery could not be prepared; the current journal is unchanged.", kind=phase) from exc
            if isinstance(exc, BackupError):
                raise
            raise BackupError("Could not prepare the backup for restore. The current journal is unchanged.") from exc

    def cancel_restore(self) -> None:
        if self._plan is not None:
            try:
                self._plan.staged.unlink(missing_ok=True)
            except OSError:
                self.backups.warning = "A temporary restore file could not be removed. The safety backup is preserved."
            self._plan = None

    def _reopen(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.connection = connect_database(self.database)
        # Keep the same application service so all views share the new repository.
        self.episodes.repository = EpisodeRepository(self.connection, device_id=self._device_id)

    def _lock_for_restore(self) -> None:
        self.connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        self.connection.execute("BEGIN EXCLUSIVE")
        self.connection.commit()
        # EXCLUSIVE locking mode retains the lock outside a transaction, so the
        # backup API can own its destination transaction without a writer race.

    def restore(self, plan: RestorePlan) -> None:
        if plan is None or plan is not self._plan or self.connection is None:
            raise BackupError("Prepare and confirm a backup before restoring it.")
        attempted = False
        locking_requested = False
        try:
            if self.connection.in_transaction:
                raise BackupError("Finish the active journal transaction before restoring.")
            if file_checksum(plan.staged) != plan.checksum:
                raise BackupError("The prepared snapshot changed. The current journal is unchanged.")
            validate_snapshot(plan.staged)
            safety = self.backups.inspect(plan.safety.path)
            if not safety.valid or safety.checksum != plan.safety.checksum:
                raise BackupError("The pre-restore safety backup changed or could not be verified. Prepare the restore again; the journal is unchanged.")
            locking_requested = True
            self._lock_for_restore()
            if content_hash(self.connection) != plan.safety.content_hash:
                raise BackupError("The journal changed after the safety backup. Cancel and prepare the restore again.")
            with closing(readonly(plan.staged)) as source:
                attempted = True
                # SQLite performs an atomic destination transaction. This also
                # handles WAL databases and other SQLite readers safely, without
                # replacing a live file underneath their open connections.
                copy_sqlite(source, self.connection)
            _, restored_hash = validate_connection(self.connection)
            if restored_hash != plan.content_hash:
                raise BackupError("The restored journal did not match the selected snapshot.")
            self._reopen()
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            if not attempted:
                if locking_requested:
                    self.connection.rollback()
                    self._reopen()
                raise BackupError(str(exc)) from exc
            try:
                unchanged = self.connection is not None and content_hash(self.connection) == plan.safety.content_hash
                if not unchanged:
                    safety = self.backups.inspect(plan.safety.path)
                    if not safety.valid or safety.checksum != plan.safety.checksum:
                        raise BackupError("The pre-restore safety backup could not be verified.")
                    if self.connection is None:
                        self.connection = sqlite3.connect(self.database, isolation_level=None)
                    self._lock_for_restore()
                    with closing(readonly(safety.path)) as source:
                        copy_sqlite(source, self.connection)
                    _, recovered_hash = validate_connection(self.connection)
                    if recovered_hash != plan.safety.content_hash:
                        raise BackupError("The recovered journal did not match the safety snapshot.")
                self._reopen()
                if content_hash(self.connection) != plan.safety.content_hash:
                    raise BackupError("The recovered journal did not match the safety snapshot.")
            except (sqlite3.Error, OSError, RuntimeError, ValueError) as recovery_error:
                if self.connection is not None:
                    self.connection.close()
                    self.connection = None
                raise BackupError(
                    f"Restore failed and the journal could not be reopened safely. Close Zymptom. "
                    f"The pre-restore safety backup is preserved at {plan.safety.path}."
                ) from recovery_error
            raise BackupError("Restore failed. The previous working journal was recovered; the safety backup is preserved.") from exc
        finally:
            self.cancel_restore()
