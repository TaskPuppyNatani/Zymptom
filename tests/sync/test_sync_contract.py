import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from zymptom.sync.contract import Change, SyncBatch, SyncError


def change_for(episode):
    return Change.build(record_id=episode.id, episode=episode, device_id=UUID(int=1),
                        operation="create", updated_at=episode.updated_at, change_id=UUID(int=2))


def test_canonical_wire_round_trip_preserves_values_order_and_privacy(full_episode):
    full_episode.notes = 'Café ☕ 雪\n"Second line"'
    full_episode.duration_minutes = 0
    full_episode.recovery_required = False
    full_episode.recovery_minutes = None
    full_episode.symptom_notes = None
    change = change_for(full_episode)
    encoded = SyncBatch((change,)).to_json()
    decoded = SyncBatch.from_json(encoded)
    assert decoded == SyncBatch((change,))
    assert decoded.changes[0].episode == full_episode
    assert decoded.changes[0].episode.functional_demands == ["sitting", "concentration", "sitting"]
    assert "雪" in encoded and "\\u96ea" not in encoded
    assert '"contract_version":1' in encoded and '"record_type":"functional_episode"' in encoded
    assert '"duration_minutes":0' in encoded and '"recovery_required":false' in encoded
    assert '"recovery_minutes":null' in encoded
    assert SyncBatch.from_json(encoded).to_json() == encoded
    assert full_episode.notes not in repr(change)
    detached = decoded.changes[0].episode
    detached.notes = "Changed outside immutable history"
    assert decoded.changes[0].episode == full_episode


def test_wire_order_is_independent_of_input_batch_order(full_episode):
    first = change_for(full_episode)
    episode = replace(full_episode, id=UUID(int=3), activity="Other")
    second = change_for(episode)
    second = replace(second, change_id=UUID(int=4))
    assert SyncBatch((first, second)).to_json() == SyncBatch((second, first)).to_json()
    assert SyncBatch.from_json(SyncBatch((first, first)).to_json()).changes == (first,)


@pytest.mark.parametrize("target,version", [("contract_version", 0), ("contract_version", 2), ("contract_version", True), ("record_schema_version", 2)])
def test_unsupported_versions_are_never_guessed(full_episode, target, version):
    batch = json.loads(SyncBatch((change_for(full_episode),)).to_json())
    batch[target] = version
    with pytest.raises(SyncError, match="Unsupported"):
        SyncBatch.from_json(json.dumps(batch))


@pytest.mark.parametrize("mutation", ["id", "timestamp", "hash", "unknown", "missing", "boolean_revision", "deleted", "payload_id", "tag_type", "duplicate_parents", "future_change", "huge_revision", "huge_minutes"])
def test_malformed_change_is_rejected(full_episode, mutation):
    data = change_for(full_episode).to_dict()
    if mutation == "id":
        data["change_id"] = "not a UUID"
    elif mutation == "timestamp":
        data["updated_at"] = "2026-01-01T00:00:00"
    elif mutation == "hash":
        data["content_hash"] = "wrong"
    elif mutation == "unknown":
        data["unknown"] = True
    elif mutation == "missing":
        del data["episode"]["notes"]
    elif mutation == "boolean_revision":
        data["revision"] = True
    elif mutation == "deleted":
        data["deleted"] = True
    elif mutation == "payload_id":
        data["episode"]["id"] = str(UUID(int=999))
    elif mutation == "tag_type":
        data["episode"]["symptoms"] = [123]
    elif mutation == "duplicate_parents":
        data["parent_ids"] = [str(UUID(int=10)), str(UUID(int=10))]
    elif mutation == "huge_revision":
        data["revision"] = 2**63
    elif mutation == "huge_minutes":
        data["episode"]["duration_minutes"] = 2**63
    else:
        data["contract_version"] = 99
    with pytest.raises(SyncError):
        Change.from_dict(data)


@pytest.mark.parametrize("value", ['{"contract_version":1,"contract_version":1}', '{broken', 'NaN', '[]', '{}'])
def test_malformed_json_never_reaches_storage(value):
    with pytest.raises(SyncError):
        SyncBatch.from_json(value)


def test_tombstone_wire_has_no_active_payload(full_episode):
    parent = change_for(full_episode)
    deleted = Change.build(record_id=full_episode.id, episode=None, device_id=UUID(int=1),
                           operation="delete", parents=(parent,), updated_at=datetime(2026, 10, 25, tzinfo=UTC))
    assert SyncBatch.from_json(SyncBatch((deleted,)).to_json()).changes[0] == deleted
    assert deleted.deleted and deleted.episode is None
