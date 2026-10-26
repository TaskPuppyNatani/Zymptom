"""Version 1 immutable revision DAG and canonical JSON, separate from exports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4, uuid5

from zymptom.domain import FunctionalEpisode
from zymptom.domain.validation import deserialize_timestamp, parse_id, serialize_timestamp, validate_episode

CONTRACT_VERSION = 1
RECORD_SCHEMA_VERSION = 1
RECORD_TYPE = "functional_episode"
LEGACY_NAMESPACE = UUID("17895883-494d-515b-a733-cc7f59c27e5c")
TIME_FIELDS = {"occurred_at", "created_at", "updated_at"}


class SyncError(ValueError):
    """Invalid/unsupported sync data or an unsafe reconciliation request."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SyncError("Duplicate JSON field names are not permitted")
        result[key] = value
    return result


def parse_json(value: str):
    try:
        return json.loads(value, object_pairs_hook=_object, parse_constant=lambda _: (_ for _ in ()).throw(SyncError("Invalid JSON number")))
    except (ValueError, TypeError, RecursionError) as exc:
        raise SyncError("Malformed sync JSON") from exc


def episode_data(episode: FunctionalEpisode) -> dict:
    validate_episode(episode)
    result = {}
    for item in fields(episode):
        value = getattr(episode, item.name)
        if item.name in TIME_FIELDS:
            value = serialize_timestamp(value)
        elif isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, Enum):
            value = value.value
        elif isinstance(value, list):
            value = value.copy()
        result[item.name] = value
    return result


def episode_from_data(value: dict) -> FunctionalEpisode:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(FunctionalEpisode)}:
        raise SyncError("The episode snapshot has missing or unknown fields")
    try:
        data = value.copy()
        for name in TIME_FIELDS:
            if not isinstance(data[name], str):
                raise SyncError("Episode timestamps must be aware ISO strings")
            data[name] = deserialize_timestamp(data[name])
        for name in ("duration_minutes", "recovery_minutes"):
            if type(data[name]) is int and data[name] > 9223372036854775807:
                raise SyncError("Minute values exceed the supported SQLite range")
        if not isinstance(data["id"], str):
            raise SyncError("The episode ID must be a UUID string")
        return FunctionalEpisode(**data)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SyncError("Invalid episode snapshot") from exc


def value_hash(record_id: UUID, episode: FunctionalEpisode | None) -> str:
    # updated_at is local audit bookkeeping, not a meaningful user edit.
    value = episode_data(episode) if episode is not None else {"id": str(record_id), "deleted": True, "record_type": RECORD_TYPE}
    if episode is not None:
        value.pop("updated_at")
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Change:
    change_id: UUID
    record_id: UUID
    revision: int
    parent_ids: tuple[UUID, ...]
    operation: str
    updated_at: datetime
    device_id: UUID
    deleted: bool
    content_hash: str
    episode_json: str | None = field(repr=False)

    @property
    def episode(self) -> FunctionalEpisode | None:
        return episode_from_data(parse_json(self.episode_json)) if self.episode_json is not None else None

    def to_dict(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION, "record_schema_version": RECORD_SCHEMA_VERSION,
            "record_type": RECORD_TYPE, "change_id": str(self.change_id), "record_id": str(self.record_id),
            "revision": self.revision, "parent_ids": [str(value) for value in self.parent_ids],
            "operation": self.operation, "updated_at": serialize_timestamp(self.updated_at),
            "device_id": str(self.device_id), "deleted": self.deleted, "content_hash": self.content_hash,
            "episode": parse_json(self.episode_json) if self.episode_json is not None else None,
        }

    @classmethod
    def build(cls, *, record_id: UUID, episode: FunctionalEpisode | None, device_id: UUID,
              operation: str, parents: tuple[Change, ...] = (), updated_at: datetime,
              change_id: UUID | None = None) -> Change:
        candidate = cls(change_id or uuid4(), record_id, max((parent.revision for parent in parents), default=0) + 1,
                        tuple(sorted((parent.change_id for parent in parents), key=str)), operation,
                        updated_at, device_id, episode is None, value_hash(record_id, episode),
                        canonical(episode_data(episode)) if episode is not None else None)
        return cls.from_dict(candidate.to_dict())

    @classmethod
    def from_dict(cls, value: dict) -> Change:
        expected = {"contract_version", "record_schema_version", "record_type", "change_id", "record_id",
                    "revision", "parent_ids", "operation", "updated_at", "device_id", "deleted", "content_hash", "episode"}
        if not isinstance(value, dict) or set(value) != expected:
            raise SyncError("Sync change fields do not match contract v1")
        if type(value["contract_version"]) is not int or value["contract_version"] != CONTRACT_VERSION:
            raise SyncError("Unsupported sync contract version")
        if type(value["record_schema_version"]) is not int or value["record_schema_version"] != RECORD_SCHEMA_VERSION:
            raise SyncError("Unsupported sync record schema version")
        try:
            if value["record_type"] != RECORD_TYPE or value["operation"] not in {"create", "update", "delete", "resolve"}:
                raise SyncError("Unsupported record type or operation")
            if type(value["revision"]) is not int or not 1 <= value["revision"] <= 9223372036854775807 or type(value["deleted"]) is not bool:
                raise SyncError("Invalid revision or tombstone state")
            if not isinstance(value["parent_ids"], list) or not all(isinstance(item, str) for item in value["parent_ids"]):
                raise SyncError("Parent IDs must be UUID strings")
            for name in ("change_id", "record_id", "device_id", "updated_at"):
                if not isinstance(value[name], str):
                    raise SyncError("IDs and timestamps must be strings")
            parents = tuple(sorted((parse_id(item) for item in value["parent_ids"]), key=str))
            if len(parents) != len(set(parents)):
                raise SyncError("Duplicate parent IDs")
            record_id = parse_id(value["record_id"])
            episode = episode_from_data(value["episode"]) if value["episode"] is not None else None
            updated = deserialize_timestamp(value["updated_at"])
            if value["deleted"] != (episode is None) or (episode and (episode.id != record_id or episode.updated_at != updated)):
                raise SyncError("Snapshot identity, timestamp, or tombstone does not match its metadata")
            if value["operation"] in {"create", "update"} and episode is None or value["operation"] == "delete" and episode is not None:
                raise SyncError("Operation and snapshot disagree")
            if value["content_hash"] != value_hash(record_id, episode):
                raise SyncError("The sync content hash does not match its snapshot")
            return cls(parse_id(value["change_id"]), record_id, value["revision"], parents, value["operation"], updated,
                       parse_id(value["device_id"]), value["deleted"], value["content_hash"],
                       canonical(episode_data(episode)) if episode is not None else None)
        except (ValueError, TypeError, OverflowError) as exc:
            raise SyncError("Invalid sync change") from exc


@dataclass(frozen=True)
class SyncBatch:
    changes: tuple[Change, ...] = ()

    def to_json(self) -> str:
        ordered = sorted(self.changes, key=lambda change: (change.revision, str(change.record_id), str(change.change_id)))
        return canonical({"contract_version": CONTRACT_VERSION, "record_schema_version": RECORD_SCHEMA_VERSION,
                          "changes": [change.to_dict() for change in ordered]})

    @classmethod
    def from_json(cls, value: str) -> SyncBatch:
        data = parse_json(value)
        if not isinstance(data, dict) or set(data) != {"contract_version", "record_schema_version", "changes"}:
            raise SyncError("Sync batch fields do not match contract v1")
        if type(data["contract_version"]) is not int or data["contract_version"] != CONTRACT_VERSION:
            raise SyncError("Unsupported sync contract version")
        if type(data["record_schema_version"]) is not int or data["record_schema_version"] != RECORD_SCHEMA_VERSION:
            raise SyncError("Unsupported sync record schema version")
        if not isinstance(data["changes"], list):
            raise SyncError("Changes must be a JSON list")
        changes = {}
        for item in data["changes"]:
            change = Change.from_dict(item)
            if change.change_id in changes and changes[change.change_id] != change:
                raise SyncError("A change ID cannot identify different revisions")
            changes[change.change_id] = change
        return cls(tuple(sorted(changes.values(), key=lambda change: (change.revision, str(change.record_id), str(change.change_id)))))


def legacy_change(episode: FunctionalEpisode) -> Change:
    identity = uuid5(LEGACY_NAMESPACE, canonical(episode_data(episode)))
    return Change.build(record_id=episode.id, episode=episode, device_id=UUID(int=0),
                        operation="create", updated_at=episode.updated_at, change_id=identity)
