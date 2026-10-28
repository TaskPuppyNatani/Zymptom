"""Revision history, heads, tombstones, and conflicts in the journal transaction."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from zymptom.domain.validation import deserialize_timestamp, parse_id

from .contract import Change, SyncError, canonical, legacy_change, parse_json, value_hash
from .identity import parse_device_id

CONFLICT_NAMESPACE = UUID("99454a4c-5f39-588b-b0ce-d00f477690d4")


def read_changes(connection) -> dict[UUID, Change]:
    result = {}
    for row in connection.execute("SELECT change_id, record_id, revision, wire_json FROM sync_changes"):
        change = Change.from_dict(parse_json(row[3]))
        if (row[0], row[1], row[2]) != (str(change.change_id), str(change.record_id), change.revision):
            raise SyncError("Stored sync revision indexes are inconsistent")
        result[change.change_id] = change
    return result


def get_head(connection, record_id: UUID) -> Change | None:
    row = connection.execute(
        "SELECT wire_json FROM sync_changes JOIN sync_heads USING(change_id) WHERE sync_heads.record_id = ?",
        (str(record_id),),
    ).fetchone()
    return Change.from_dict(parse_json(row[0])) if row else None


def insert_change(connection, change: Change) -> None:
    if not connection.in_transaction:
        raise RuntimeError("Sync writes require the caller's transaction")
    connection.execute("INSERT INTO sync_changes VALUES (?, ?, ?, ?)",
                       (str(change.change_id), str(change.record_id), change.revision, canonical(change.to_dict())))


def set_head(connection, change: Change) -> None:
    connection.execute(
        "INSERT INTO sync_heads VALUES (?, ?) ON CONFLICT(record_id) DO UPDATE SET change_id = excluded.change_id",
        (str(change.record_id), str(change.change_id)),
    )


def assert_editable(connection, record_id: UUID) -> None:
    if connection.execute("SELECT 1 FROM sync_conflicts WHERE record_id = ? AND resolution_id IS NULL LIMIT 1", (str(record_id),)).fetchone():
        raise SyncError("This episode has an unresolved sync conflict. Resolve it explicitly before editing or deleting it.")


def track_local(connection, *, episode, before, record_id: UUID, device_id: UUID, operation: str) -> Change:
    assert_editable(connection, record_id)
    head = get_head(connection, record_id)
    if operation == "create" and head is not None:
        raise SyncError("This record ID already has sync history, possibly a tombstone. Use a new episode ID.")
    if head is None and before is not None:
        head = legacy_change(before)
        insert_change(connection, head)
        set_head(connection, head)
    if head is not None and head.deleted:
        raise SyncError("A deleted episode cannot be revived by an ordinary edit")
    if head is not None and episode is not None and value_hash(record_id, episode) == head.content_hash:
        return head
    updated_at = episode.updated_at if episode is not None else max(datetime.now(UTC), head.updated_at + timedelta(microseconds=1))
    change = Change.build(record_id=record_id, episode=episode, device_id=device_id,
                          operation=operation, parents=(head,) if head else (), updated_at=updated_at)
    insert_change(connection, change)
    set_head(connection, change)
    return change


def seed_legacy(connection) -> tuple[Change, ...]:
    from zymptom.persistence.repository import EpisodeRepository
    created = []
    for episode in EpisodeRepository(connection).list():
        if get_head(connection, episode.id) is None:
            change = legacy_change(episode)
            insert_change(connection, change)
            set_head(connection, change)
            created.append(change)
    return tuple(created)


def read_enrollment(connection) -> tuple[UUID, str, datetime] | None:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sync_enrollment'").fetchone():
        return None
    rows = connection.execute("SELECT singleton, journal_id, state, enrolled_at FROM sync_enrollment").fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][2] != "local_only":
        raise SyncError("Journal sync enrollment metadata is invalid")
    if not isinstance(rows[0][1], str) or not isinstance(rows[0][3], str):
        raise SyncError("Journal sync enrollment metadata is invalid")
    try:
        journal_id = parse_device_id(rows[0][1])
        enrolled_at = deserialize_timestamp(rows[0][3])
    except (ValueError, TypeError) as exc:
        raise SyncError("Journal sync enrollment metadata is invalid") from exc
    if any(isinstance(row[0], str) and row[0].startswith("firebase-sandbox:")
           for row in connection.execute("SELECT DISTINCT provider FROM sync_acknowledgements")):
        raise SyncError("Sandbox sync state cannot be enrolled for production sync")
    return journal_id, rows[0][2], enrolled_at


def ancestors(change_id: UUID, changes: dict[UUID, Change]) -> set[UUID]:
    found = set()
    pending = [change_id]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(changes[current].parent_ids)
    return found


def frontier(record_id: UUID, changes: dict[UUID, Change]) -> tuple[Change, ...]:
    versions = {identity: change for identity, change in changes.items() if change.record_id == record_id}
    parents = {parent for change in versions.values() for parent in change.parent_ids}
    return tuple(versions[identity] for identity in sorted(versions.keys() - parents, key=str))


def validate_graph(changes: dict[UUID, Change]) -> None:
    for change in sorted(changes.values(), key=lambda item: (item.revision, str(item.change_id))):
        if any(parent not in changes for parent in change.parent_ids):
            raise SyncError("A parent revision is missing; deliver its history first or in the same batch")
        parents = [changes[identity] for identity in change.parent_ids]
        if any(parent.record_id != change.record_id or parent.revision >= change.revision for parent in parents):
            raise SyncError("Revision ancestry is cyclic or belongs to another record")
        if change.revision != max((parent.revision for parent in parents), default=0) + 1:
            raise SyncError("Revision generation does not match its parents")
        if change.operation == "create":
            if parents or change.deleted:
                raise SyncError("Create revisions must be active roots")
        elif change.operation in {"update", "delete"}:
            if len(parents) != 1 or parents[0].deleted:
                raise SyncError("Ordinary edits/deletes require one active parent; tombstones cannot resurrect")
            if not change.deleted and change.episode.created_at != parents[0].episode.created_at:
                raise SyncError("An update cannot change the original creation timestamp")
            if change.operation == "update" and change.content_hash == parents[0].content_hash:
                raise SyncError("Metadata-only edits must not create new revisions")
        else:
            if len(parents) < 2:
                raise SyncError("An explicit conflict resolution requires concurrent parents")
            if any(left.change_id in ancestors(right.change_id, changes) for left in parents for right in parents if left != right):
                raise SyncError("Resolution parents must be concurrent, not redundant ancestors")
            if not any(parent.deleted == change.deleted and parent.content_hash == change.content_hash for parent in parents):
                raise SyncError("A resolution must explicitly choose one of its parent versions")
        if change.device_id.int == 0:
            if change.operation != "create" or change.episode is None or change != legacy_change(change.episode):
                raise SyncError("The zero origin is reserved for deterministic legacy baseline revisions")


def add_conflict(connection, local: Change, incoming: Change, changes: dict[UUID, Change]) -> UUID:
    pair = sorted((str(local.change_id), str(incoming.change_id)))
    identity = uuid5(CONFLICT_NAMESPACE, canonical(pair))
    common = ancestors(local.change_id, changes) & ancestors(incoming.change_id, changes)
    base = max(common, key=lambda item: (changes[item].revision, str(item))) if common else None
    connection.execute("INSERT OR IGNORE INTO sync_conflicts VALUES (?, ?, ?, ?, ?, NULL)",
                       (str(identity), str(local.record_id), str(local.change_id), str(incoming.change_id), str(base) if base else None))
    return identity


def resolve_covered_conflicts(connection, resolution: Change, changes: dict[UUID, Change]) -> None:
    if resolution.operation != "resolve":
        return
    covered = ancestors(resolution.change_id, changes)
    for row in connection.execute("SELECT conflict_id, local_id, incoming_id FROM sync_conflicts WHERE record_id = ? AND resolution_id IS NULL", (str(resolution.record_id),)).fetchall():
        if UUID(row[1]) in covered and UUID(row[2]) in covered:
            connection.execute("UPDATE sync_conflicts SET resolution_id = ? WHERE conflict_id = ?", (str(resolution.change_id), row[0]))


def validate_state(connection, *, allow_untracked: bool = False) -> None:
    """Backup verification includes revision history, head consistency, and conflicts."""
    from zymptom.persistence.repository import EpisodeRepository
    enrollment = read_enrollment(connection)
    if enrollment is not None:
        allow_untracked = False
    changes = read_changes(connection)
    validate_graph(changes)
    heads = {}
    repository = EpisodeRepository(connection)
    for row in connection.execute("SELECT record_id, change_id FROM sync_heads"):
        try:
            record_id, change_id = parse_id(row[0]), parse_id(row[1])
        except ValueError as exc:
            raise SyncError("A sync head is missing or inconsistent") from exc
        head = changes.get(change_id)
        if head is None or head.record_id != record_id or head not in frontier(record_id, changes):
            raise SyncError("A sync head is missing or inconsistent")
        episode = repository.get(record_id)
        if head.deleted != (episode is None) or head.content_hash != value_hash(record_id, episode):
            raise SyncError("Episode contents and sync head disagree")
        heads[record_id] = head
    if {change.record_id for change in changes.values()} - heads.keys():
        raise SyncError("A record has no sync head")
    if not allow_untracked and ({episode.id for episode in repository.list()} - heads.keys()):
        raise SyncError("A record has no sync head")
    unresolved = []
    for row in connection.execute("SELECT conflict_id, record_id, local_id, incoming_id, base_id, resolution_id FROM sync_conflicts"):
        try:
            conflict_id, record_id = parse_id(row[0]), parse_id(row[1])
            local_id, incoming_id = parse_id(row[2]), parse_id(row[3])
            base_id = parse_id(row[4]) if row[4] is not None else None
            resolution_id = parse_id(row[5]) if row[5] is not None else None
        except ValueError as exc:
            raise SyncError("Conflict identity or concurrent candidates are inconsistent") from exc
        local, incoming = changes.get(local_id), changes.get(incoming_id)
        if local is None or incoming is None or record_id not in heads:
            raise SyncError("Conflict candidates are missing from sync history")
        if local.record_id != record_id or incoming.record_id != record_id:
            raise SyncError("Conflict candidates belong to different records")
        left, right = ancestors(local.change_id, changes), ancestors(incoming.change_id, changes)
        pair = sorted((str(local.change_id), str(incoming.change_id)))
        if conflict_id != uuid5(CONFLICT_NAMESPACE, canonical(pair)) or local.change_id in right or incoming.change_id in left:
            raise SyncError("Conflict identity or concurrent candidates are inconsistent")
        common = left & right
        if base_id is not None and base_id not in common:
            raise SyncError("Conflict base is not a common ancestor")
        if resolution_id is not None:
            resolution = changes.get(resolution_id)
            if resolution is None or resolution.operation != "resolve" or not {local.change_id, incoming.change_id} <= ancestors(resolution.change_id, changes):
                raise SyncError("Conflict resolution does not preserve both candidates")
        else:
            if {local.change_id, incoming.change_id} <= ancestors(heads[record_id].change_id, changes):
                raise SyncError("A covered conflict was not marked resolved")
            unresolved.append((local.change_id, incoming.change_id))
    for record_id, head in heads.items():
        head_history = ancestors(head.change_id, changes)
        for tip in frontier(record_id, changes):
            if tip.change_id == head.change_id:
                continue
            tip_history = ancestors(tip.change_id, changes)
            if not any((left in head_history and right in tip_history) or (right in head_history and left in tip_history)
                       for left, right in unresolved):
                raise SyncError("A concurrent candidate has no explicit unresolved conflict")
    for row in connection.execute("SELECT provider, change_id FROM sync_acknowledgements"):
        try:
            acknowledged_id = parse_id(row[1])
        except ValueError as exc:
            raise SyncError("A provider acknowledgement is invalid") from exc
        if not isinstance(row[0], str) or not row[0].strip() or len(row[0]) > 128 or acknowledged_id not in changes:
            raise SyncError("A provider acknowledgement is invalid")
