"""Complete FunctionalEpisode CRUD, including ordered child collections."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Iterator
from uuid import UUID

from zymptom.domain.enums import EpisodeOutcome
from zymptom.domain.models import FunctionalEpisode
from zymptom.domain.validation import (
    deserialize_timestamp,
    parse_id,
    serialize_timestamp,
    unique_tags,
    validate_episode,
)

from .database import transaction

SCALAR_FIELDS = (
    "id", "occurred_at", "activity", "outcome", "duration_minutes", "symptom_notes",
    "help_level", "help_notes", "recovery_required", "recovery_minutes",
    "resume_ability", "notes", "created_at", "updated_at",
)
TAG_TABLES = {
    "functional_demands": "episode_functional_demands",
    "symptoms": "episode_symptoms",
    "adaptations": "episode_adaptations",
    "consequences": "episode_consequences",
    "recovery_actions": "episode_recovery_actions",
}


class EpisodeNotFoundError(KeyError):
    """An update targeted an episode that does not exist."""


class EpisodeRepository:
    """Use a connection returned by connect_database; the caller owns its lifetime.

    Writes own their transaction and reject an already-active transaction.
    Lists are oldest first, with UUID as the deterministic tie-breaker.
    """

    def __init__(self, connection: sqlite3.Connection, *, device_id: UUID | None = None) -> None:
        from zymptom.sync.identity import parse_device_id
        self.connection = connection
        self._device_id = parse_device_id(device_id) if device_id is not None else None
        connection.create_function("zymptom_casefold", 1, str.casefold, deterministic=True)

    @property
    def device_id(self) -> UUID:
        from zymptom.sync.identity import DeviceIdentity
        return self._device_id if self._device_id is not None else DeviceIdentity().get()

    def create(self, episode: FunctionalEpisode) -> FunctionalEpisode:
        from zymptom.sync.storage import track_local
        validate_episode(episode)
        values = self._scalar_values(episode)
        with transaction(self.connection):
            self.connection.execute(
                f"INSERT INTO episodes ({', '.join(SCALAR_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in SCALAR_FIELDS)})",
                tuple(values[field] for field in SCALAR_FIELDS),
            )
            tags = self._insert_tags(episode)
            track_local(self.connection, episode=replace(episode, **tags), before=None,
                        record_id=episode.id, device_id=self.device_id, operation="create")
        for field, field_values in tags.items():
            setattr(episode, field, field_values)
        return episode

    def get(self, episode_id: UUID | str) -> FunctionalEpisode | None:
        key = str(parse_id(episode_id))
        # A read transaction prevents scalar and tag queries seeing different
        # snapshots if another connection writes between the queries.
        with self._read_snapshot():
            row = self.connection.execute("SELECT * FROM episodes WHERE id = ?", (key,)).fetchone()
            return self._load(row) if row is not None else None

    def update(self, episode: FunctionalEpisode) -> FunctionalEpisode:
        from zymptom.sync.storage import track_local
        validate_episode(episode)
        values = self._scalar_values(episode)
        key = str(episode.id)
        with transaction(self.connection):
            before = self.get(episode.id)
            previous = self.connection.execute(
                "SELECT created_at, updated_at FROM episodes WHERE id = ?", (key,)
            ).fetchone()
            if previous is None:
                raise EpisodeNotFoundError(key)
            created_at = deserialize_timestamp(previous["created_at"])
            # Guarantee advancement even for rapid writes or a clock adjustment.
            updated_at = max(
                datetime.now(UTC),
                deserialize_timestamp(previous["updated_at"]) + timedelta(microseconds=1),
            )
            values["updated_at"] = serialize_timestamp(updated_at)
            mutable_fields = [field for field in SCALAR_FIELDS if field not in ("id", "created_at")]
            self.connection.execute(
                f"UPDATE episodes SET {', '.join(f'{field} = ?' for field in mutable_fields)} WHERE id = ?",
                (*[values[field] for field in mutable_fields], key),
            )
            for table in TAG_TABLES.values():
                self.connection.execute(f"DELETE FROM {table} WHERE episode_id = ?", (key,))
            tags = self._insert_tags(episode)
            track_local(self.connection, episode=replace(episode, **tags, created_at=created_at, updated_at=updated_at),
                        before=before, record_id=episode.id, device_id=self.device_id, operation="update")
        # Normalize the caller's tags and timestamps only after the write commits.
        for field, field_values in tags.items():
            setattr(episode, field, field_values)
        episode.created_at = created_at
        episode.updated_at = updated_at
        return episode

    def delete(self, episode_id: UUID | str) -> bool:
        from zymptom.sync.storage import track_local
        key = str(parse_id(episode_id))
        with transaction(self.connection):
            before = self.get(key)
            if before is not None:
                track_local(self.connection, episode=None, before=before, record_id=before.id,
                            device_id=self.device_id, operation="delete")
            cursor = self.connection.execute("DELETE FROM episodes WHERE id = ?", (key,))
        return cursor.rowcount > 0

    def list(self) -> list[FunctionalEpisode]:
        with self._read_snapshot():
            rows = self.connection.execute("SELECT * FROM episodes ORDER BY occurred_at ASC, id ASC").fetchall()
            return [self._load(row) for row in rows]

    def search(
        self, *, activity: str = "", outcome: EpisodeOutcome | None = None,
        functional_demand: str | None = None, occurred_from: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> list[FunctionalEpisode]:
        """Find episodes newest first. Time bounds are inclusive/exclusive UTC.

        Activity is a literal, Unicode-case-insensitive substring. Demands match
        exact stored tags; EXISTS avoids duplicate episodes for duplicate tags.
        """
        clauses = []
        parameters = []
        if activity.strip():
            clauses.append("instr(zymptom_casefold(episodes.activity), ?) > 0")
            parameters.append(activity.strip().casefold())
        if outcome is not None:
            clauses.append("episodes.outcome = ?")
            parameters.append(EpisodeOutcome(outcome).value)
        if functional_demand is not None:
            clauses.append("""EXISTS (
                SELECT 1 FROM episode_functional_demands AS demands
                WHERE demands.episode_id = episodes.id AND demands.tag = ?
            )""")
            parameters.append(functional_demand)
        start = serialize_timestamp(occurred_from) if occurred_from is not None else None
        end = serialize_timestamp(occurred_before) if occurred_before is not None else None
        if start is not None and end is not None and start >= end:
            raise ValueError("The start must be earlier than the exclusive end.")
        if start is not None:
            clauses.append("episodes.occurred_at >= ?")
            parameters.append(start)
        if end is not None:
            clauses.append("episodes.occurred_at < ?")
            parameters.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read_snapshot():
            rows = self.connection.execute(
                f"SELECT episodes.* FROM episodes {where} ORDER BY occurred_at DESC, id DESC",
                parameters,
            ).fetchall()
            return [self._load(row) for row in rows]

    def functional_demands(self) -> list[str]:
        """Return distinct persisted demand values for the filter choices."""
        rows = self.connection.execute("SELECT DISTINCT tag FROM episode_functional_demands").fetchall()
        return sorted((row[0] for row in rows), key=lambda value: (value.casefold(), value))

    def _insert_tags(self, episode: FunctionalEpisode, *, normalize: bool = True) -> dict[str, list[str]]:
        tags = {field: unique_tags(getattr(episode, field)) if normalize else list(getattr(episode, field)) for field in TAG_TABLES}
        for field, table in TAG_TABLES.items():
            self.connection.executemany(
                f"INSERT INTO {table} (episode_id, position, tag) VALUES (?, ?, ?)",
                [(str(episode.id), position, tag) for position, tag in enumerate(tags[field])],
            )
        return tags

    def _apply_sync_snapshot(self, record_id: UUID, episode: FunctionalEpisode | None) -> None:
        """Apply an already validated immutable snapshot inside the merge owner."""
        if not self.connection.in_transaction:
            raise RuntimeError("Sync snapshots require a merge transaction")
        if episode is not None:
            validate_episode(episode)
            if episode.id != record_id:
                raise ValueError("Sync snapshot identity mismatch")
        self.connection.execute("DELETE FROM episodes WHERE id = ?", (str(record_id),))
        if episode is not None:
            values = self._scalar_values(episode)
            self.connection.execute(
                f"INSERT INTO episodes ({', '.join(SCALAR_FIELDS)}) VALUES ({', '.join('?' for _ in SCALAR_FIELDS)})",
                tuple(values[field] for field in SCALAR_FIELDS),
            )
            # Legacy snapshots may contain duplicates. Replicate faithfully;
            # an ordinary later save still normalizes them through update().
            self._insert_tags(episode, normalize=False)

    @staticmethod
    def _scalar_values(episode: FunctionalEpisode) -> dict[str, object]:
        values = {field: getattr(episode, field) for field in SCALAR_FIELDS}
        values["id"] = str(episode.id)
        for field in ("occurred_at", "created_at", "updated_at"):
            values[field] = serialize_timestamp(getattr(episode, field))
        for field in ("outcome", "help_level", "resume_ability"):
            value = getattr(episode, field)
            values[field] = value.value if value is not None else None
        if episode.recovery_required is not None:
            values["recovery_required"] = int(episode.recovery_required)
        return values

    def _load(self, row: sqlite3.Row) -> FunctionalEpisode:
        fields = dict(row)
        for field in ("occurred_at", "created_at", "updated_at"):
            fields[field] = deserialize_timestamp(fields[field])
        if fields["recovery_required"] is not None:
            fields["recovery_required"] = bool(fields["recovery_required"])
        for field, table in TAG_TABLES.items():
            fields[field] = [
                tag_row[0] for tag_row in self.connection.execute(
                    f"SELECT tag FROM {table} WHERE episode_id = ? ORDER BY position", (row["id"],)
                )
            ]
        return FunctionalEpisode(**fields)

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        """Reuse a caller's transaction, or own a deferred read transaction."""
        if self.connection.in_transaction:
            yield
            return
        self.connection.execute("BEGIN")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
