"""Own the journal connection and orchestrate recoverable local restores."""

from __future__ import annotations

import sqlite3
import os
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from zymptom.domain.validation import serialize_timestamp
from zymptom.persistence import EpisodeRepository, connect_database
from zymptom.persistence.backups import (
    BackupError, BackupInfo, BackupStore, content_hash, copy_sqlite, file_checksum,
    readonly, sync_directory, transaction_content_hash, validate_connection, validate_snapshot,
)
from zymptom.persistence.database import transaction
from zymptom.persistence.migrations import apply_migrations

from .episodes import EpisodeService
from zymptom.sync.contract import SyncBatch, SyncError
from zymptom.sync.identity import DeviceIdentity, parse_device_id
from zymptom.sync.service import MergeResult, SyncService
from zymptom.sync.storage import ancestors, frontier, get_head, read_changes, read_enrollment, seed_legacy, validate_state


PRODUCTION_PROVIDER_PREFIX = "firebase-production:"


def production_provider(journal_id: UUID | str) -> str:
    return f"{PRODUCTION_PROVIDER_PREFIX}{parse_device_id(journal_id)}"


@dataclass(frozen=True)
class EnrollmentPreview:
    enrolled: bool
    journal_id: UUID | None
    enrolled_at: datetime | None
    entry_count: int
    already_tracked_count: int
    bootstrap_required_count: int
    safety_backup_required: bool

    @property
    def already_enrolled(self) -> bool:
        return self.enrolled

    @property
    def state(self) -> str:
        return "local_only" if self.enrolled else "not_enrolled"


@dataclass(frozen=True)
class EnrollmentResult:
    journal_id: UUID
    enrolled_at: datetime
    entry_count: int
    bootstrapped_count: int
    already_tracked_count: int
    already_enrolled: bool
    safety_backup: Path | None


@dataclass(frozen=True)
class ProductionSyncPreview:
    enrolled: bool
    journal_id: UUID | None
    provider: str | None
    entry_count: int
    total_change_count: int
    pending_change_count: int
    acknowledged_change_count: int
    conflict_count: int
    first_sync_required: bool
    safety_backup_required: bool


@dataclass(frozen=True)
class ProductionSyncResult:
    journal_id: UUID
    provider: str
    mailbox_state: str
    uploaded: tuple[UUID, ...]
    merge: MergeResult
    conflicts: int
    up_to_date: bool
    safety_backup: Path | None


def _classify_mailbox(connection: sqlite3.Connection, batch: SyncBatch, known: dict, combined: dict) -> str:
    if not batch.changes:
        return "empty"
    new_ids = {change.change_id for change in batch.changes if change.change_id not in known}
    if not new_ids:
        return "identical"
    incoming_map = {change.change_id: change for change in batch.changes}
    for record_id in {change.record_id for change in batch.changes if change.change_id in new_ids}:
        for candidate in frontier(record_id, incoming_map):
            if candidate.change_id not in new_ids:
                continue
            head = get_head(connection, record_id)
            if (head is not None
                    and head.change_id not in ancestors(candidate.change_id, combined)
                    and candidate.change_id not in ancestors(head.change_id, combined)):
                return "conflicting"
    return "compatible"


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


class ManualBackupError(BackupError):
    def __init__(self, message: str, *, kind: str = "write"):
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

    @property
    def journal_id(self) -> UUID | None:
        return self.inspect_enrollment().journal_id

    def inspect_enrollment(self) -> EnrollmentPreview:
        """Inspect local Z5 enrollment state and bootstrap needs without mutating the journal."""
        if self.connection is None:
            raise SyncError("The current journal is unavailable.")
        if (self.database.parent / ".sandbox-owner.json").exists() or self.database.parent.name == "sandbox-sync":
            raise SyncError("Sandbox journals cannot be enrolled for production sync.")
        with self.episodes.repository._read_snapshot():
            if any(isinstance(row[0], str) and row[0].startswith("firebase-sandbox:")
                   for row in self.connection.execute("SELECT DISTINCT provider FROM sync_acknowledgements")):
                raise SyncError("Sandbox sync state cannot be enrolled for production sync.")
            validate_state(self.connection, allow_untracked=True)
            enrollment = read_enrollment(self.connection)
            episodes = self.episodes.repository.list()
            heads = {UUID(row[0]) for row in self.connection.execute("SELECT record_id FROM sync_heads")}
            already_tracked = sum(1 for episode in episodes if episode.id in heads)
            bootstrap_required = len(episodes) - already_tracked
            return EnrollmentPreview(
                enrolled=enrollment is not None,
                journal_id=enrollment[0] if enrollment is not None else None,
                enrolled_at=enrollment[2] if enrollment is not None else None,
                entry_count=len(episodes),
                already_tracked_count=already_tracked,
                bootstrap_required_count=bootstrap_required,
                safety_backup_required=not (enrollment is not None and bootstrap_required == 0),
            )

    def enroll_sync(self, *, now: datetime | None = None) -> EnrollmentResult:
        """Create a verified safety backup and atomically enroll the production journal for local Z5 sync."""
        if self.connection is None:
            raise SyncError("The current journal is unavailable.")
        if self.connection.in_transaction:
            raise SyncError("Finish the active journal transaction before enrolling sync.")
        preview = self.inspect_enrollment()
        if preview.enrolled and preview.bootstrap_required_count == 0:
            return EnrollmentResult(
                journal_id=preview.journal_id,
                enrolled_at=preview.enrolled_at,
                entry_count=preview.entry_count,
                bootstrapped_count=0,
                already_tracked_count=preview.already_tracked_count,
                already_enrolled=True,
                safety_backup=None,
            )
        timestamp = now if now is not None else datetime.now(UTC)
        if timestamp.utcoffset() is None:
            raise ValueError("Enrollment time must have a timezone")
        timestamp = timestamp.astimezone(UTC)
        safety = self.before_destructive_change()
        if not isinstance(safety, BackupInfo) or not safety.valid or file_checksum(safety.path) != safety.checksum:
            raise BackupError("A verified pre-enrollment safety backup is required.")
        _, snapshot_hash = validate_snapshot(safety.path)
        if snapshot_hash != safety.content_hash or snapshot_hash != content_hash(self.connection):
            raise BackupError("The safety backup does not match the current journal.")
        with transaction(self.connection):
            if transaction_content_hash(self.connection) != safety.content_hash:
                raise SyncError("The journal changed after its safety backup. Retry sync setup.")
            current = self.inspect_enrollment()
            created = seed_legacy(self.connection)
            if current.enrolled:
                journal_id = current.journal_id
                enrolled_at = current.enrolled_at
            else:
                journal_id = uuid4()
                while journal_id.int == 0 or journal_id == self._device_id:
                    journal_id = uuid4()
                enrolled_at = timestamp
                self.connection.execute(
                    "INSERT INTO sync_enrollment (singleton, journal_id, state, enrolled_at) VALUES (1, ?, 'local_only', ?)",
                    (str(journal_id), serialize_timestamp(enrolled_at)),
                )
            validate_state(self.connection, allow_untracked=False)
        return EnrollmentResult(
            journal_id=journal_id,
            enrolled_at=enrolled_at,
            entry_count=current.entry_count,
            bootstrapped_count=len(created),
            already_tracked_count=current.already_tracked_count,
            already_enrolled=current.enrolled,
            safety_backup=safety.path,
        )

    def inspect_production_sync(self, *, has_synced_before: bool = False) -> ProductionSyncPreview:
        """Inspect production sync readiness and pending Change counts without network calls or mutations."""
        enrollment = self.inspect_enrollment()
        if not enrollment.enrolled or enrollment.journal_id is None or enrollment.bootstrap_required_count > 0:
            return ProductionSyncPreview(
                enrolled=False,
                journal_id=None,
                provider=None,
                entry_count=enrollment.entry_count,
                total_change_count=0,
                pending_change_count=0,
                acknowledged_change_count=0,
                conflict_count=0,
                first_sync_required=True,
                safety_backup_required=True,
            )
        with self.episodes.repository._read_snapshot():
            validate_state(self.connection, allow_untracked=False)
            provider = production_provider(enrollment.journal_id)
            service = SyncService(self.episodes, before_merge=self.before_destructive_change)
            changes = read_changes(self.connection)
            pending = service.pending(provider)
            acknowledged = len(changes) - len(pending.changes)
            conflicts = len(service.unresolved_conflicts())
            first_sync_required = (acknowledged == 0 and not has_synced_before)
            return ProductionSyncPreview(
                enrolled=True,
                journal_id=enrollment.journal_id,
                provider=provider,
                entry_count=enrollment.entry_count,
                total_change_count=len(changes),
                pending_change_count=len(pending.changes),
                acknowledged_change_count=acknowledged,
                conflict_count=conflicts,
                first_sync_required=first_sync_required,
                safety_backup_required=True,
            )

    def sync_production(
        self,
        mailbox,
        *,
        approve_first_sync: bool = False,
        has_synced_before: bool = False,
    ) -> ProductionSyncResult:
        """Conservatively synchronize the enrolled production journal with its Firebase mailbox."""
        if self.connection is None:
            raise SyncError("The current journal is unavailable.")
        if self.connection.in_transaction:
            raise SyncError("Finish the active journal transaction before syncing.")
        preview = self.inspect_production_sync(has_synced_before=has_synced_before)
        if not preview.enrolled or preview.journal_id is None or preview.provider is None:
            raise SyncError("Prepare this journal for sync before connecting.")
        is_first_sync = preview.first_sync_required
        if is_first_sync and not approve_first_sync:
            raise SyncError("Confirm the first production sync before uploading this journal.")
        if getattr(mailbox, "identity_path", None) is not None:
            raise SyncError("Production sync cannot use the disposable sandbox mailbox.")
        if getattr(mailbox, "journal_id", None) is None:
            mailbox.journal_id = str(preview.journal_id)
        connected = mailbox.connect() if hasattr(mailbox, "connect") else mailbox.journal_id
        try:
            connected_id = parse_device_id(connected)
            mailbox_id = parse_device_id(getattr(mailbox, "journal_id", connected))
        except (ValueError, TypeError) as exc:
            raise SyncError("The mailbox journal identity is invalid.") from exc
        if connected_id != preview.journal_id or mailbox_id != preview.journal_id:
            raise SyncError("The mailbox journal identity does not match this production journal.")
        service = SyncService(self.episodes, before_merge=self.before_destructive_change)
        provider = preview.provider
        incoming = mailbox.pull()
        batch = SyncBatch.from_json(incoming.to_json() if isinstance(incoming, SyncBatch) else incoming)
        with self.episodes.repository._read_snapshot():
            known, combined = service._check(batch)
            mailbox_state = _classify_mailbox(self.connection, batch, known, combined)
        if batch.changes:
            merge = service.apply(batch, source_provider=provider)
            safety_backup = merge.safety_backup
        elif is_first_sync:
            safety = service._safety_backup()
            merge = MergeResult(safety_backup=safety.path)
            safety_backup = safety.path
        else:
            merge = MergeResult()
            safety_backup = None
        pending = service.pending(provider)
        uploaded = []
        for change in pending.changes:
            mailbox.upload(change)
            service.acknowledge(provider, [change.change_id])
            uploaded.append(change.change_id)
        remaining = service.pending(provider)
        conflicts = len(service.unresolved_conflicts())
        return ProductionSyncResult(
            journal_id=preview.journal_id,
            provider=provider,
            mailbox_state=mailbox_state,
            uploaded=tuple(uploaded),
            merge=merge,
            conflicts=conflicts,
            up_to_date=(not remaining.changes and conflicts == 0),
            safety_backup=safety_backup,
        )

    def prepare_restore(self, path: Path) -> RestorePlan:
        if self.connection is None:
            raise BackupError("The journal is unavailable. Close Zymptom before trying again.")
        self.cancel_restore()
        info = self.backups.inspect(path)
        if not info.valid:
            raise BackupError(info.error)
        return self._prepare_snapshot(info, readonly)

    def create_manual_backup(self, path: Path, *, overwrite: bool = False) -> int:
        """Publish a verified, standalone recovery-compatible snapshot at a chosen path."""
        if self.connection is None:
            raise ManualBackupError("The current journal is unavailable.")
        destination = Path(path).absolute()

        def check_destination():
            protected = [self.database, *(Path(str(self.database) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
            if any(destination.resolve() == item.resolve() or
                   (destination.exists() and item.exists() and os.path.samefile(destination, item))
                   for item in protected):
                raise ManualBackupError("Choose a different location.", kind="current")
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise ManualBackupError("Choose a regular file location.")
            if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise ManualBackupError("The selected file may be in use.")

        temporary = None
        try:
            source = next(row[2] for row in self.connection.execute("PRAGMA database_list") if row[1] == "main")
            if not source or Path(source).resolve() != self.database:
                raise ManualBackupError("The backup connection does not belong to this journal.")
            check_destination()
            if destination.exists() and not overwrite:
                raise ManualBackupError("The destination already exists.", kind="exists")
            original = destination.stat() if destination.exists() else None
            fd, name = tempfile.mkstemp(prefix=".zymptom-journal-", suffix=".tmp", dir=destination.parent)
            os.close(fd)
            temporary = Path(name)
            with closing(sqlite3.connect(temporary, isolation_level=None)) as target:
                copy_sqlite(self.connection, target)
                target.execute("PRAGMA journal_mode = DELETE")
            try:
                preview = self.inspect_recovery(temporary)
            except RecoveryError as exc:
                raise ManualBackupError("The backup could not be verified.", kind="verify") from exc
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            check_destination()
            if original is not None:
                if not destination.exists() or destination.stat() != original:
                    raise ManualBackupError("The destination changed while backing up.", kind="exists")
                os.replace(temporary, destination)
            else:
                # Same-directory link publishes without clobbering a file that appeared meanwhile.
                os.link(temporary, destination)
                temporary.unlink()
            sync_directory(destination.parent)
            return preview.entry_count
        except ManualBackupError:
            raise
        except FileExistsError as exc:
            raise ManualBackupError("The destination already exists.", kind="exists") from exc
        except (OSError, sqlite3.Error, BackupError, ValueError, StopIteration) as exc:
            raise ManualBackupError("Could not create the journal backup.") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

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


def sync_production(
    session: JournalSession,
    mailbox,
    *,
    approve_first_sync: bool = False,
    has_synced_before: bool = False,
) -> ProductionSyncResult:
    return session.sync_production(
        mailbox,
        approve_first_sync=approve_first_sync,
        has_synced_before=has_synced_before,
    )
