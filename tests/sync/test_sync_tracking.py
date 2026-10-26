from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
from uuid import UUID

import pytest

from zymptom.domain import EpisodeOutcome
from zymptom.sync.contract import SyncError
from zymptom.sync.identity import DeviceIdentity
from zymptom.sync.simulator import DeviceStore
from zymptom.sync.storage import read_changes


def test_persistent_identity_survives_restart_and_explicit_reset(tmp_path):
    path = tmp_path / "installation" / "device.json"
    first = DeviceIdentity(path).get()
    assert isinstance(first, UUID) and first.int != 0
    assert DeviceIdentity(path).get() == first
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"version": 1, "device_id": str(first)}
    second = DeviceIdentity(path).reset()
    assert second != first and DeviceIdentity(path).get() == second


def test_concurrent_identity_initialization_creates_only_one_identity(tmp_path):
    path = tmp_path / "identity" / "device.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: DeviceIdentity(path).get(), range(8)))
    assert len(set(identities)) == 1


@pytest.mark.parametrize("data", ["{bad", "[]", '{"version":true,"device_id":"00000000-0000-0000-0000-000000000001"}', '{"version":1,"device_id":123}'])
def test_corrupt_identity_requires_intentional_reset(tmp_path, data):
    path = tmp_path / "device.json"
    path.write_text(data)
    with pytest.raises(ValueError, match="Reset it explicitly"):
        DeviceIdentity(path).get()
    assert path.read_text() == data
    assert DeviceIdentity(path).reset().int != 0


def test_local_create_edit_noop_delete_and_id_reuse(simulator):
    device = simulator.desktop
    episode = device.episodes.record(activity="Local attempt", outcome=EpisodeOutcome.STOPPED_EARLY)
    created = device.sync.known_revision(episode.id)
    assert created.revision == 1 and not created.parent_ids and created.operation == "create"
    assert created.device_id == device.identity.get() and created.episode == episode
    episode.notes = "Meaningful edit"
    device.episodes.update(episode)
    edited = device.sync.known_revision(episode.id)
    assert edited.revision == 2 and edited.parent_ids == (created.change_id,)
    audit_before = episode.updated_at
    device.episodes.update(episode)
    assert episode.updated_at > audit_before
    assert device.sync.known_revision(episode.id) == edited
    assert len(read_changes(device.session.connection)) == 2
    saved = deepcopy(episode)
    assert device.episodes.delete(episode.id)
    tombstone = device.sync.known_revision(episode.id)
    assert tombstone.revision == 3 and tombstone.operation == "delete" and tombstone.deleted
    assert tombstone.parent_ids == (edited.change_id,)
    assert device.episodes.get(episode.id) is None and device.episodes.list() == []
    assert not device.episodes.delete(episode.id)
    assert device.sync.known_revision(episode.id) == tombstone
    with pytest.raises(SyncError, match="already has sync history"):
        device.episodes.repository.create(saved)
    assert device.episodes.get(episode.id) is None


def test_pending_acknowledgements_are_provider_scoped_and_survive_restart(simulator):
    device = simulator.desktop
    episode = device.episodes.record(activity="An attempt", outcome=EpisodeOutcome.COMPLETED_NORMALLY)
    before = device.sync.known_revision(episode.id)
    batch = device.sync.pending("provider-a")
    device.sync.acknowledge("provider-a", [before.change_id])
    device.sync.acknowledge("provider-a", [before.change_id])
    assert device.sync.pending("provider-a").changes == ()
    assert device.sync.pending("provider-b") == batch
    assert device.sync.known_revision(episode.id) == before
    root = device.root
    identity = device.identity.get()
    device.close()
    reopened = DeviceStore(root)
    try:
        assert reopened.identity.get() == identity
        assert reopened.sync.known_revision(episode.id) == before
        assert reopened.sync.pending("provider-a").changes == ()
        assert reopened.sync.pending("provider-b") == batch
    finally:
        reopened.close()


def test_installation_reset_does_not_rewrite_old_origins(repository, full_episode):
    repository.create(full_episode)
    from zymptom.sync.storage import get_head
    old = get_head(repository.connection, full_episode.id)
    new_identity = DeviceIdentity().reset()
    full_episode.notes = "After intentional device reset"
    repository.update(full_episode)
    new = get_head(repository.connection, full_episode.id)
    assert new.device_id == new_identity and new.device_id != old.device_id
    assert new.parent_ids == (old.change_id,)


def test_local_metadata_failure_rolls_back_edit_and_delete(simulator):
    device = simulator.desktop
    episode = device.episodes.record(activity="Keep", outcome=EpisodeOutcome.STOPPED_EARLY)
    before = deepcopy(episode)
    head = device.sync.known_revision(episode.id)
    device.session.connection.execute("CREATE TRIGGER reject_revision BEFORE INSERT ON sync_changes BEGIN SELECT RAISE(ABORT, 'revision failure'); END")
    episode.notes = "Draft must not partially persist"
    for operation in (lambda: device.episodes.update(episode), lambda: device.episodes.delete(episode.id)):
        with pytest.raises(sqlite3.IntegrityError, match="revision failure"):
            operation()
        assert device.episodes.get(episode.id) == before
        assert device.sync.known_revision(episode.id) == head
        assert episode.updated_at == before.updated_at


def test_unresolved_conflict_details_survive_restart(simulator, shared):
    for device, notes in ((simulator.desktop, "Desktop edit"), (simulator.phone, "Phone edit")):
        episode = device.episodes.get(shared)
        episode.notes = notes
        device.episodes.update(episode)
    simulator.exchange()
    device = simulator.desktop
    conflict, = device.sync.unresolved_conflicts()
    identity = device.identity.get()
    root = device.root
    device.close()
    reopened = DeviceStore(root)
    try:
        assert reopened.identity.get() == identity
        assert reopened.sync.unresolved_conflicts() == (conflict,)
        assert reopened.sync.inspect_conflict(conflict.conflict_id) == conflict
    finally:
        reopened.close()


def test_reserved_device_identity_is_rejected_before_creating_journal(tmp_path, repository):
    from zymptom.persistence.repository import EpisodeRepository
    from zymptom.services.journal import JournalSession
    with pytest.raises(ValueError, match="reserved zero"):
        EpisodeRepository(repository.connection, device_id=UUID(int=0))
    path = tmp_path / "must-not-create.sqlite3"
    with pytest.raises(ValueError, match="reserved zero"):
        JournalSession(path, device_id=UUID(int=0))
    assert not path.exists()


def test_unknown_acknowledgement_batch_is_atomic(simulator):
    device = simulator.desktop
    episode = device.episodes.record(activity="Known", outcome=EpisodeOutcome.STOPPED_EARLY)
    head = device.sync.known_revision(episode.id)
    with pytest.raises(SyncError, match="unknown"):
        device.sync.acknowledge("provider", [head.change_id, UUID(int=999)])
    assert device.sync.pending("provider").changes == (head,)
