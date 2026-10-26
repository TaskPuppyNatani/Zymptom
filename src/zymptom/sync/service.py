"""Atomic local reconciliation, acknowledgements, and explicit conflict choice."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from zymptom.domain.validation import parse_id
from zymptom.persistence.backups import BackupInfo, content_hash, file_checksum, transaction_content_hash, validate_snapshot
from zymptom.persistence.database import transaction
from zymptom.services import EpisodeService

from .contract import Change, SyncBatch, SyncError
from .storage import (
    add_conflict, ancestors, frontier, get_head, insert_change, read_changes,
    resolve_covered_conflicts, set_head, validate_graph, validate_state,
)


@dataclass(frozen=True)
class MergeResult:
    applied: tuple[UUID, ...] = ()
    stale: tuple[UUID, ...] = ()
    duplicates: tuple[UUID, ...] = ()
    conflicts: tuple[UUID, ...] = ()
    safety_backup: Path | None = None


@dataclass(frozen=True)
class Conflict:
    conflict_id: UUID
    record_id: UUID
    local: Change
    incoming: Change
    base: Change | None
    current_head: Change
    candidates: tuple[Change, ...]
    resolution: Change | None


def provider_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise SyncError("A provider checkpoint needs a non-blank namespace of at most 128 characters")
    return value


class SyncService:
    def __init__(self, episodes: EpisodeService, *, before_merge):
        self.episodes = episodes
        self.before_merge = before_merge

    @property
    def repository(self):
        return self.episodes.repository

    @property
    def connection(self):
        return self.repository.connection

    def known_revision(self, record_id: UUID | str) -> Change | None:
        with self.repository._read_snapshot():
            return get_head(self.connection, parse_id(record_id))

    def pending(self, provider: str) -> SyncBatch:
        provider = provider_key(provider)
        with self.repository._read_snapshot():
            changes = read_changes(self.connection)
            acknowledged = {UUID(row[0]) for row in self.connection.execute(
                "SELECT change_id FROM sync_acknowledgements WHERE provider = ?", (provider,),
            )}
            return SyncBatch.from_json(SyncBatch(tuple(change for identity, change in changes.items() if identity not in acknowledged)).to_json())

    def acknowledge(self, provider: str, change_ids) -> None:
        provider = provider_key(provider)
        identities = sorted({parse_id(identity) for identity in change_ids}, key=str)
        with transaction(self.connection):
            known = read_changes(self.connection)
            if any(identity not in known for identity in identities):
                raise SyncError("Cannot acknowledge an unknown change")
            self._acknowledge(provider, identities)

    def _acknowledge(self, provider, identities):
        self.connection.executemany("INSERT OR IGNORE INTO sync_acknowledgements VALUES (?, ?)",
                                    [(provider, str(identity)) for identity in identities])

    def _check(self, batch):
        known = read_changes(self.connection)
        combined = known.copy()
        for change in batch.changes:
            if change.change_id in known and known[change.change_id] != change:
                raise SyncError("An immutable change ID was reused with different contents")
            combined[change.change_id] = change
        validate_graph(combined)
        validate_state(self.connection)
        return known, combined

    def _safety_backup(self) -> BackupInfo:
        if self.connection.in_transaction:
            raise SyncError("A sync merge must own its database transaction")
        info = self.before_merge()
        if not isinstance(info, BackupInfo) or not info.valid or file_checksum(info.path) != info.checksum:
            raise SyncError("A verified pre-merge safety backup is required")
        _, snapshot_hash = validate_snapshot(info.path)
        if snapshot_hash != info.content_hash or snapshot_hash != content_hash(self.connection):
            raise SyncError("The safety backup does not match the current journal")
        return info

    def apply(self, incoming: SyncBatch | str, *, source_provider: str | None = None) -> MergeResult:
        batch = SyncBatch.from_json(incoming.to_json() if isinstance(incoming, SyncBatch) else incoming)
        provider = provider_key(source_provider) if source_provider is not None else None
        if self.connection.in_transaction:
            raise SyncError("A sync merge must own its database transaction")
        if not batch.changes:
            return MergeResult()
        with self.repository._read_snapshot():
            self._check(batch)
        safety = self._safety_backup()
        applied, stale, duplicates, conflicts = set(), set(), set(), set()
        with transaction(self.connection):
            if transaction_content_hash(self.connection) != safety.content_hash:
                raise SyncError("The journal changed after its safety backup. Retry the merge.")
            # Recheck after taking the write lock; another local writer might
            # have committed while the safety snapshot was being produced.
            known, combined = self._check(batch)
            new = {change.change_id: change for change in batch.changes if change.change_id not in known}
            duplicates.update(change.change_id for change in batch.changes if change.change_id in known)
            for change in new.values():
                insert_change(self.connection, change)
            incoming_map = {change.change_id: change for change in batch.changes}
            for record_id in sorted({change.record_id for change in new.values()}, key=str):
                for candidate in frontier(record_id, incoming_map):
                    if candidate.change_id not in new:
                        continue
                    head = get_head(self.connection, record_id)
                    if head is None or head.change_id in ancestors(candidate.change_id, combined):
                        self.repository._apply_sync_snapshot(record_id, candidate.episode)
                        set_head(self.connection, candidate)
                        applied.add(candidate.change_id)
                    elif candidate.change_id in ancestors(head.change_id, combined):
                        stale.add(candidate.change_id)
                    else:
                        conflicts.add(add_conflict(self.connection, head, candidate, combined))
            for change in new.values():
                resolve_covered_conflicts(self.connection, change, combined)
            if provider is not None:
                self._acknowledge(provider, (change.change_id for change in batch.changes))
            validate_state(self.connection)
        ordered = lambda values: tuple(sorted(values, key=str))
        return MergeResult(ordered(applied), ordered(stale), ordered(duplicates), ordered(conflicts), safety.path)

    def _conflict(self, row, changes):
        conflict_id, record_id, local, incoming, base, resolution = row
        record_id = UUID(record_id)
        return Conflict(UUID(conflict_id), record_id, changes[UUID(local)], changes[UUID(incoming)],
                        changes[UUID(base)] if base else None, get_head(self.connection, record_id),
                        frontier(record_id, changes), changes[UUID(resolution)] if resolution else None)

    def unresolved_conflicts(self) -> tuple[Conflict, ...]:
        with self.repository._read_snapshot():
            changes = read_changes(self.connection)
            return tuple(self._conflict(tuple(row), changes) for row in self.connection.execute(
                "SELECT * FROM sync_conflicts WHERE resolution_id IS NULL ORDER BY record_id, conflict_id"))

    def inspect_conflict(self, conflict_id: UUID | str) -> Conflict | None:
        with self.repository._read_snapshot():
            row = self.connection.execute("SELECT * FROM sync_conflicts WHERE conflict_id = ?", (str(parse_id(conflict_id)),)).fetchone()
            return self._conflict(tuple(row), read_changes(self.connection)) if row else None

    def resolve(self, record_id: UUID | str, *, chosen_change_id: UUID | str) -> Change:
        record_id, chosen_id = parse_id(record_id), parse_id(chosen_change_id)
        self._resolution_candidates(record_id, chosen_id)
        safety = self._safety_backup()
        with transaction(self.connection):
            if transaction_content_hash(self.connection) != safety.content_hash:
                raise SyncError("The journal changed after its safety backup. Inspect the conflict again.")
            changes, candidates = self._resolution_candidates(record_id, chosen_id)
            chosen = changes[chosen_id]
            updated = max(datetime.now(UTC), max(item.updated_at for item in candidates) + timedelta(microseconds=1))
            episode = replace(chosen.episode, updated_at=updated) if chosen.episode is not None else None
            resolution = Change.build(record_id=record_id, episode=episode, device_id=self.repository.device_id,
                                      operation="resolve", parents=candidates, updated_at=updated)
            insert_change(self.connection, resolution)
            changes[resolution.change_id] = resolution
            validate_graph(changes)
            self.repository._apply_sync_snapshot(record_id, episode)
            set_head(self.connection, resolution)
            resolve_covered_conflicts(self.connection, resolution, changes)
            validate_state(self.connection)
            return resolution

    def _resolution_candidates(self, record_id, chosen_id):
        changes = read_changes(self.connection)
        candidates = frontier(record_id, changes)
        unresolved = self.connection.execute("SELECT 1 FROM sync_conflicts WHERE record_id = ? AND resolution_id IS NULL", (str(record_id),)).fetchone()
        if not unresolved or len(candidates) < 2 or chosen_id not in {item.change_id for item in candidates}:
            raise SyncError("Inspect the current conflict candidates and explicitly choose a current version")
        return changes, candidates
