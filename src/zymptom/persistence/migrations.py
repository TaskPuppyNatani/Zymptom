"""Numbered, append-only schema migrations executed without executescript.

Using execute for each statement keeps SQLite DDL inside our transaction;
executescript would implicitly commit a pending transaction.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Sequence

from .database import transaction


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATION_001 = Migration(
    version=1,
    name="create_functional_episodes",
    statements=(
        """
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY NOT NULL,
            occurred_at TEXT NOT NULL,
            activity TEXT NOT NULL CHECK (length(trim(activity)) > 0),
            outcome TEXT NOT NULL CHECK (outcome IN (
                'completed_normally', 'completed_with_difficulty',
                'completed_with_adaptation', 'completed_with_help',
                'stopped_early', 'unable_to_complete', 'avoided_due_to_limitation'
            )),
            duration_minutes INTEGER CHECK (
                duration_minutes IS NULL OR
                (typeof(duration_minutes) = 'integer' AND duration_minutes >= 0)
            ),
            symptom_notes TEXT,
            help_level TEXT CHECK (help_level IN (
                'none', 'some', 'significant', 'other_person_completed_part',
                'other_person_completed_all'
            )),
            help_notes TEXT,
            recovery_required INTEGER CHECK (recovery_required IN (0, 1)),
            recovery_minutes INTEGER CHECK (
                recovery_minutes IS NULL OR
                (typeof(recovery_minutes) = 'integer' AND recovery_minutes >= 0)
            ),
            resume_ability TEXT CHECK (resume_ability IN (
                'immediately', 'after_short_break', 'later_same_day', 'next_day',
                'could_not_resume', 'unknown'
            )),
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX episodes_chronological ON episodes (occurred_at, id)",
        *(
            f"""
            CREATE TABLE {table} (
                episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position >= 0),
                tag TEXT NOT NULL CHECK (length(trim(tag)) > 0),
                PRIMARY KEY (episode_id, position)
            )
            """
            for table in (
                "episode_functional_demands",
                "episode_symptoms",
                "episode_adaptations",
                "episode_consequences",
                "episode_recovery_actions",
            )
        ),
    ),
)

MIGRATIONS = (MIGRATION_001,)


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Apply pending versions in order and record them in schema_migrations.

    All pending migrations and their history are committed as one unit. Unknown
    versions or mismatched history fail explicitly, avoiding accidental downgrade.
    Existing migration definitions must never be edited after release.
    """
    ordered = sorted(migrations, key=lambda migration: migration.version)
    versions = [migration.version for migration in ordered]
    if any(type(version) is not int or version < 1 for version in versions):
        raise ValueError("Migration versions must be positive integers")
    if len(versions) != len(set(versions)):
        raise ValueError("Migration versions must be unique")

    with transaction(connection):
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        expected_prefix = [(migration.version, migration.name) for migration in ordered[:len(applied)]]
        if [tuple(row) for row in applied] != expected_prefix:
            raise RuntimeError("Database migration history is incompatible with this application")
        for migration in ordered[len(applied):]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat(timespec="microseconds")),
            )
    return ordered[-1].version if ordered else 0


def schema_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    return connection.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()[0]
