from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import json
import sqlite3
from uuid import UUID

import pytest

from zymptom.domain import EpisodeOutcome
from zymptom.persistence.backups import BackupError, content_hash
from zymptom.sync.contract import Change, SyncBatch, SyncError
from zymptom.sync.storage import read_changes
from zymptom.sync import service as sync_service_module


def test_independent_creates_converge_and_stop_echoing(simulator):
    a, b = simulator.desktop, simulator.phone
    a.episodes.record(activity="Desktop attempt", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
    b.episodes.record(activity="Phone attempt", outcome=EpisodeOutcome.STOPPED_EARLY)
    simulator.exchange()
    assert a.episodes.list() == b.episodes.list() and len(a.episodes.list()) == 2
    assert not a.sync.unresolved_conflicts() and not b.sync.unresolved_conflicts()
    assert not a.sync.pending("phone").changes and not b.sync.pending("desktop").changes
    assert not any(result.applied for result in simulator.exchange())


def test_one_sided_update_and_complete_batch_duplicate_delivery(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    episode = a.episodes.get(shared)
    episode.notes = "Café ☕\n雪 preserved"
    episode.functional_demands = ["walking", "sitting", "memory"]
    a.episodes.update(episode)
    batch = a.sync.pending("phone")
    result = b.sync.apply(batch.to_json(), source_provider="desktop")
    assert result.applied and result.safety_backup.is_file()
    assert b.episodes.get(shared) == episode
    before = content_hash(b.session.connection)
    for _ in range(3):
        duplicate = b.sync.apply(batch.to_json(), source_provider="desktop")
        assert not duplicate.applied and not duplicate.conflicts
        assert duplicate.duplicates == tuple(change.change_id for change in batch.changes)
    assert content_hash(b.session.connection) == before
    assert b.sync.known_revision(shared) == a.sync.known_revision(shared)


def test_ancestry_wins_even_with_older_wall_clock(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    parent = a.sync.known_revision(shared)
    episode = replace(parent.episode, notes="Descendant with a slow clock", updated_at=datetime(2000, 1, 1, tzinfo=UTC))
    change = Change.build(record_id=shared, episode=episode, device_id=a.identity.get(), operation="update", parents=(parent,), updated_at=episode.updated_at)
    b.sync.apply(SyncBatch((change,)))
    assert b.sync.known_revision(shared) == change and b.episodes.get(shared).notes == episode.notes


def test_delete_unchanged_and_stale_active_delivery_never_resurrect(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    original = a.sync.known_revision(shared)
    a.episodes.delete(shared)
    b.sync.apply(a.sync.pending("phone"))
    tombstone = b.sync.known_revision(shared)
    assert b.episodes.get(shared) is None and tombstone.deleted
    for _ in range(2):
        result = b.sync.apply(SyncBatch((original,)))
        assert result.duplicates == (original.change_id,)
        assert b.episodes.get(shared) is None and b.sync.known_revision(shared) == tombstone
    assert not b.sync.unresolved_conflicts()


def divergent_edits(simulator, shared):
    for device, notes in ((simulator.desktop, "Desktop candidate ☕"), (simulator.phone, "Phone candidate\n雪")):
        episode = device.episodes.get(shared)
        episode.notes = notes
        device.episodes.update(episode)
    simulator.exchange()


def test_concurrent_edit_candidates_base_and_idempotent_conflicts(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    base = a.sync.known_revision(shared)
    divergent_edits(simulator, shared)
    left, = a.sync.unresolved_conflicts()
    right, = b.sync.unresolved_conflicts()
    assert left.conflict_id == right.conflict_id
    assert left.local.episode.notes == "Desktop candidate ☕"
    assert left.incoming.episode.notes == "Phone candidate\n雪"
    assert left.base == base and right.base == base
    assert left.local.operation == left.incoming.operation == "update"
    assert left.local.device_id != left.incoming.device_id
    assert a.episodes.get(shared) != b.episodes.get(shared)
    before = content_hash(a.session.connection)
    a.sync.apply(SyncBatch((left.incoming,)))
    assert content_hash(a.session.connection) == before
    assert a.sync.inspect_conflict(left.conflict_id) == left
    with pytest.raises(SyncError, match="unresolved sync conflict"):
        a.episodes.update(a.episodes.get(shared))
    with pytest.raises(SyncError, match="unresolved sync conflict"):
        a.episodes.delete(shared)


def test_explicit_edit_choice_creates_common_descendant_and_converges(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    divergent_edits(simulator, shared)
    conflict, = a.sync.unresolved_conflicts()
    resolution = a.sync.resolve(shared, chosen_change_id=conflict.incoming.change_id)
    assert resolution.operation == "resolve" and resolution.revision == 3
    assert set(resolution.parent_ids) == {conflict.local.change_id, conflict.incoming.change_id}
    assert not a.sync.unresolved_conflicts()
    assert a.sync.inspect_conflict(conflict.conflict_id).resolution == resolution
    simulator.exchange()
    assert a.episodes.list() == b.episodes.list()
    assert a.episodes.get(shared).notes == "Phone candidate\n雪"
    assert not b.sync.unresolved_conflicts()


@pytest.mark.parametrize("choice", ["edit", "delete"])
def test_concurrent_delete_edit_preserves_both_and_requires_explicit_choice(simulator, shared, choice):
    a, b = simulator.desktop, simulator.phone
    a.episodes.delete(shared)
    edit = b.episodes.get(shared)
    edit.notes = "Meaningful offline edit"
    b.episodes.update(edit)
    simulator.exchange()
    left, = a.sync.unresolved_conflicts()
    right, = b.sync.unresolved_conflicts()
    assert left.local.deleted and not left.incoming.deleted
    assert right.incoming.deleted and not right.local.deleted
    assert a.episodes.get(shared) is None
    assert b.episodes.get(shared).notes == "Meaningful offline edit"
    selected = left.incoming if choice == "edit" else left.local
    a.sync.resolve(shared, chosen_change_id=selected.change_id)
    simulator.exchange()
    assert a.episodes.list() == b.episodes.list()
    assert (a.episodes.get(shared) is None) == (choice == "delete")
    assert a.sync.inspect_conflict(left.conflict_id).incoming.episode.notes == "Meaningful offline edit"


@pytest.mark.parametrize("kind", ["missing_parent", "bad_generation", "cross_record", "resurrection", "future", "mixed_malformed", "zero_origin_update", "zero_origin_delete", "zero_origin_create"])
def test_invalid_batches_never_mutate_or_create_safety_backups(simulator, shared, kind):
    a, b = simulator.desktop, simulator.phone
    base = b.sync.known_revision(shared)
    edited = replace(base.episode, notes="Incoming", updated_at=datetime.now(UTC))
    change = Change.build(record_id=shared, episode=edited, device_id=a.identity.get(), operation="update", parents=(base,), updated_at=edited.updated_at)
    if kind == "missing_parent":
        change = replace(change, parent_ids=(UUID(int=999),))
    elif kind == "bad_generation":
        change = replace(change, revision=77)
    elif kind == "cross_record":
        other = b.episodes.record(activity="Other record", outcome=EpisodeOutcome.STOPPED_EARLY)
        change = replace(change, parent_ids=(b.sync.known_revision(other.id).change_id,))
    elif kind == "resurrection":
        b.episodes.delete(shared)
        change = replace(change, parent_ids=(b.sync.known_revision(shared).change_id,), revision=3)
    elif kind == "zero_origin_update":
        change = replace(change, device_id=UUID(int=0))
    elif kind == "zero_origin_delete":
        change = Change.build(record_id=shared, episode=None, device_id=UUID(int=0), operation="delete",
                              parents=(base,), updated_at=edited.updated_at)
    elif kind == "zero_origin_create":
        change = Change.build(record_id=shared, episode=edited, device_id=UUID(int=0), operation="create",
                              updated_at=edited.updated_at)
    wire = SyncBatch((change,)).to_json()
    if kind == "future":
        data = json.loads(wire)
        data["contract_version"] = 99
        wire = json.dumps(data)
    elif kind == "mixed_malformed":
        data = json.loads(wire)
        data["changes"].append({"broken": True})
        wire = json.dumps(data)
    before = content_hash(b.session.connection)
    files = set(b.session.backups.directory.iterdir())
    with pytest.raises(SyncError):
        b.sync.apply(wire)
    assert content_hash(b.session.connection) == before
    assert set(b.session.backups.directory.iterdir()) == files


def test_reusing_change_identity_with_different_contents_is_rejected(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    head = b.sync.known_revision(shared)
    changed = replace(head.episode, notes="Different root contents")
    forged = Change.build(record_id=shared, episode=changed, device_id=head.device_id, operation="create",
                          updated_at=changed.updated_at, change_id=head.change_id)
    with pytest.raises(SyncError, match="reused"):
        b.sync.apply(SyncBatch((forged,)))


@pytest.mark.parametrize("failure", ["child", "acknowledgement"])
def test_merge_rolls_back_all_rows_metadata_and_receipts_on_failure(simulator, failure):
    a, b = simulator.desktop, simulator.phone
    for activity in ("One", "Two"):
        a.episodes.record(activity=activity, outcome=EpisodeOutcome.STOPPED_EARLY, symptoms=["reject_merge"])
    batch = a.sync.pending("phone")
    table = "episode_symptoms" if failure == "child" else "sync_acknowledgements"
    b.session.connection.execute(f"CREATE TRIGGER reject_merge BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected merge failure'); END")
    before = content_hash(b.session.connection)
    with pytest.raises(sqlite3.IntegrityError, match="injected merge failure"):
        b.sync.apply(batch, source_provider="desktop")
    assert content_hash(b.session.connection) == before
    assert b.episodes.list() == [] and read_changes(b.session.connection) == {}
    assert any(info.valid and info.kind == "safety" for info in b.session.backups.list())
    b.session.connection.execute("DROP TRIGGER reject_merge")
    b.sync.apply(batch, source_provider="desktop")
    assert b.episodes.list() == a.episodes.list()


def test_backup_failure_prevents_merge_and_empty_batch_needs_no_backup(simulator, monkeypatch):
    a, b = simulator.desktop, simulator.phone
    a.episodes.record(activity="An incoming record", outcome=EpisodeOutcome.STOPPED_EARLY)
    before = content_hash(b.session.connection)

    def fail():
        raise BackupError("Injected safety backup failure")

    monkeypatch.setattr(b.sync, "before_merge", fail)
    assert b.sync.apply(SyncBatch()).safety_backup is None
    with pytest.raises(BackupError):
        b.sync.apply(a.sync.pending("phone"))
    assert content_hash(b.session.connection) == before


def test_child_before_parent_in_batch_is_sorted_and_atomic(simulator):
    a, b = simulator.desktop, simulator.phone
    episode = a.episodes.record(activity="Root", outcome=EpisodeOutcome.STOPPED_EARLY)
    episode.notes = "Child"
    a.episodes.update(episode)
    batch = a.sync.pending("phone")
    b.sync.apply(SyncBatch(tuple(reversed(batch.changes))))
    assert b.episodes.list() == a.episodes.list()
    assert not b.sync.unresolved_conflicts()


def test_later_concurrent_branch_edits_are_preserved_and_stale_choice_refused(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    local = a.episodes.get(shared)
    local.notes = "Local branch"
    a.episodes.update(local)
    remote = b.episodes.get(shared)
    remote.notes = "First remote edit"
    b.episodes.update(remote)
    first_remote = b.sync.known_revision(shared)
    a.sync.apply(b.sync.pending("desktop"))
    remote.notes = "Later meaningful remote edit"
    b.episodes.update(remote)
    latest_remote = b.sync.known_revision(shared)
    a.sync.apply(b.sync.pending("desktop"))
    assert len(a.sync.unresolved_conflicts()) == 2
    assert {item.change_id for item in a.sync.unresolved_conflicts()[0].candidates} == {
        a.sync.known_revision(shared).change_id, latest_remote.change_id,
    }
    with pytest.raises(SyncError, match="current version"):
        a.sync.resolve(shared, chosen_change_id=first_remote.change_id)
    a.sync.resolve(shared, chosen_change_id=latest_remote.change_id)
    simulator.exchange()
    assert a.episodes.list() == b.episodes.list()
    assert a.episodes.get(shared).notes == "Later meaningful remote edit"
    assert not a.sync.unresolved_conflicts() and not b.sync.unresolved_conflicts()


@pytest.mark.parametrize("operation", ["identical_edits", "concurrent_deletes"])
def test_concurrent_operations_remain_explicit_even_when_contents_match(simulator, shared, operation):
    a, b = simulator.desktop, simulator.phone
    for device in (a, b):
        if operation == "concurrent_deletes":
            device.episodes.delete(shared)
        else:
            episode = device.episodes.get(shared)
            episode.notes = "Same text independently edited"
            device.episodes.update(episode)
    simulator.exchange()
    conflict, = a.sync.unresolved_conflicts()
    assert conflict.local.content_hash == conflict.incoming.content_hash
    assert conflict.local.change_id != conflict.incoming.change_id
    a.sync.resolve(shared, chosen_change_id=conflict.local.change_id)
    simulator.exchange()
    assert a.episodes.list() == b.episodes.list() and not b.sync.unresolved_conflicts()
    with pytest.raises(SyncError):
        a.sync.resolve(shared, chosen_change_id=conflict.local.change_id)


def test_independent_uuid_collision_preserves_both_roots_without_inventing_base(simulator):
    a, b = simulator.desktop, simulator.phone
    identity = UUID(int=123)
    a.episodes.record(id=identity, activity="Desktop root", outcome=EpisodeOutcome.STOPPED_EARLY)
    b.episodes.record(id=identity, activity="Phone root", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
    simulator.exchange()
    conflict, = a.sync.unresolved_conflicts()
    assert conflict.base is None
    assert conflict.local.episode.activity == "Desktop root" and conflict.incoming.episode.activity == "Phone root"


def test_wrong_journal_safety_backup_is_refused(simulator, monkeypatch):
    a, b = simulator.desktop, simulator.phone
    a.episodes.record(activity="Incoming", outcome=EpisodeOutcome.STOPPED_EARLY)
    monkeypatch.setattr(b.sync, "before_merge", a.session.before_destructive_change)
    before = content_hash(b.session.connection)
    with pytest.raises(SyncError, match="does not match"):
        b.sync.apply(a.sync.pending("phone"))
    assert content_hash(b.session.connection) == before


def test_writer_between_backup_and_merge_causes_safe_retry(simulator, monkeypatch):
    a, b = simulator.desktop, simulator.phone
    incoming = a.episodes.record(activity="Incoming", outcome=EpisodeOutcome.STOPPED_EARLY)
    original_transaction = sync_service_module.transaction

    @contextmanager
    def concurrent_write(connection):
        b.episodes.record(activity="Local write after safety snapshot", outcome=EpisodeOutcome.STOPPED_EARLY)
        with original_transaction(connection):
            yield

    monkeypatch.setattr(sync_service_module, "transaction", concurrent_write)
    with pytest.raises(SyncError, match="changed after its safety backup"):
        b.sync.apply(a.sync.pending("phone"))
    assert b.episodes.get(incoming.id) is None
    assert [episode.activity for episode in b.episodes.list()] == ["Local write after safety snapshot"]
    assert len(read_changes(b.session.connection)) == 1


def test_metadata_only_remote_update_is_rejected(simulator, shared):
    a, b = simulator.desktop, simulator.phone
    parent = b.sync.known_revision(shared)
    episode = replace(parent.episode, updated_at=datetime.now(UTC))
    incoming = Change.build(record_id=shared, episode=episode, device_id=a.identity.get(), operation="update",
                            parents=(parent,), updated_at=episode.updated_at)
    with pytest.raises(SyncError, match="Metadata-only"):
        b.sync.apply(SyncBatch((incoming,)))
