"""Complete FunctionalEpisode CRUD, including ordered child collections."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Iterator
from uuid import UUID

from zymptom.domain.models import FunctionalEpisode
from zymptom.domain.validation import (
    deserialize_timestamp,
    parse_id,
    serialize_timestamp,
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

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(self, episode: FunctionalEpisode) -> FunctionalEpisode:
        validate_episode(episode)
        values = self._scalar_values(episode)
        with transaction(self.connection):
            self.connection.execute(
                f"INSERT INTO episodes ({', '.join(SCALAR_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in SCALAR_FIELDS)})",
                tuple(values[field] for field in SCALAR_FIELDS),
            )
            self._insert_tags(episode)
        return episode

    def get(self, episode_id: UUID | str) -> FunctionalEpisode | None:
        key = str(parse_id(episode_id))
        # A read transaction prevents scalar and tag queries seeing different
        # snapshots if another connection writes between the queries.
        with self._read_snapshot():
            row = self.connection.execute("SELECT * FROM episodes WHERE id = ?", (key,)).fetchone()
            return self._load(row) if row is not None else None

    def update(self, episode: FunctionalEpisode) -> FunctionalEpisode:
        validate_episode(episode)
        values = self._scalar_values(episode)
        key = str(episode.id)
        with transaction(self.connection):
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
            self._insert_tags(episode)
        # Do not report a new timestamp in memory unless the write committed.
        episode.created_at = created_at
        episode.updated_at = updated_at
        return episode

    def delete(self, episode_id: UUID | str) -> bool:
        key = str(parse_id(episode_id))
        with transaction(self.connection):
            cursor = self.connection.execute("DELETE FROM episodes WHERE id = ?", (key,))
        return cursor.rowcount > 0

    def list(self) -> list[FunctionalEpisode]:
        with self._read_snapshot():
            rows = self.connection.execute("SELECT * FROM episodes ORDER BY occurred_at ASC, id ASC").fetchall()
            return [self._load(row) for row in rows]

    def _insert_tags(self, episode: FunctionalEpisode) -> None:
        for field, table in TAG_TABLES.items():
            self.connection.executemany(
                f"INSERT INTO {table} (episode_id, position, tag) VALUES (?, ?, ?)",
                [(str(episode.id), position, tag) for position, tag in enumerate(getattr(episode, field))],
            )

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
