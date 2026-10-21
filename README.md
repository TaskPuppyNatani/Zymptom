# Zymptom

Zymptom is a local-first functional journal. Its atomic record is a
`FunctionalEpisode`: one real-world attempt to perform an activity, including
duration, difficulties, adaptations, help, outcome, recovery, and ability to resume.

This repository implements **Z0 — Foundation only**. It contains a Python domain
model, a SQLite repository, explicit numbered migrations, a small application
service, and pytest coverage. There is no GUI or UI framework dependency, and no
AI, accounts, cloud, synchronization, reporting, or health integrations.

## Install and test

Requires Python 3.12 or newer. Runtime dependencies are entirely standard library.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

Initialize or reopen a database (pending migrations apply automatically):

```bash
python -m zymptom --database ./zymptom.sqlite3
```

The command creates a database at the chosen path; its parent directory must
already exist. The library also supports `:memory:` for ephemeral databases.

## Use the repository

```python
from contextlib import closing

from zymptom.domain import EpisodeOutcome, FunctionalEpisode, ResumeAbility
from zymptom.persistence import EpisodeRepository, connect_database

with closing(connect_database("zymptom.sqlite3")) as connection:
    repository = EpisodeRepository(connection)
    episode = FunctionalEpisode(
        activity="Homework",
        outcome=EpisodeOutcome.STOPPED_EARLY,
        duration_minutes=32,
        functional_demands=["sitting", "concentration"],
        adaptations=["changed_position"],
        recovery_required=True,
        recovery_minutes=45,
        resume_ability=ResumeAbility.LATER_SAME_DAY,
    )
    repository.create(episode)
    assert repository.get(episode.id) == episode

    episode.notes = "Resumed after resting."
    repository.update(episode)
    assert repository.get(episode.id) == episode

    episodes = repository.list()  # Oldest occurred_at first; UUID breaks ties.
    assert repository.delete(episode.id)
    assert repository.get(episode.id) is None
```

`EpisodeService(repository).record(activity=..., outcome=..., **details)` is an
application-facing shortcut that constructs and persists an episode. The service
also exposes `get`, `update`, `delete`, and `list`.

## Domain and persistence contracts

- IDs are UUID objects in the domain and UUID strings in SQLite. Constructors
  and ID lookups also accept UUID strings.
- Enums persist their stable machine-readable values. Construction accepts
  either the enum or a valid value string; subsequent edits should use enums.
- All datetimes must be timezone-aware. They default to the current UTC time,
  normalize to UTC at construction, and persist as ISO 8601 with microseconds.
  Naive datetimes are rejected to avoid guessing the user's timezone.
- Optional scalar fields default to `None`; collections default to independent
  empty lists. `None`, `False`, and `HelpLevel.NONE` retain distinct meanings.
- Activity and each tag must be non-blank strings. Durations are non-negative
  integers, excluding booleans. Text is otherwise preserved, including Unicode.
- Records are mutable and revalidated before every write. Updating retains the
  database's original `created_at` and advances `updated_at` by at least one
  microsecond relative to its stored value, even if the system clock moves back.
  Successful updates copy those timestamps back to the supplied object; failed
  writes leave its timestamps unchanged.
- Five child tables store tags with explicit list positions. Order and duplicate
  tags round-trip exactly. Foreign keys are enabled on every factory connection,
  and deleting an episode cascades to its children.
- Each write owns an explicit transaction covering scalars and all child rows.
  Write operations reject an already-active transaction. Reads use a consistent
  SQLite snapshot, reusing an existing transaction when present.
- `create` and `update` return the supplied episode. Missing reads return `None`;
  missing updates raise `EpisodeNotFoundError`; deletion returns whether a row
  existed. Creating an existing ID raises `sqlite3.IntegrityError`.
- Connections are owned by the caller. Use `closing(...)` or explicitly call
  `close()`; SQLite's connection context manager alone does not close it.

## Migrations

`src/zymptom/persistence/migrations.py` defines `MIGRATION_001` and the ordered
`MIGRATIONS` registry. Add new numbered `Migration` objects for schema changes;
do not edit a released migration. `schema_migrations` records each applied
version, name, and application timestamp. `schema_version(connection)` returns
the latest version (or zero before initialization).

Pending migrations execute in ascending version order in one transaction,
including their history entries. Reopening skips applied versions. Incompatible
history is rejected explicitly. Migration statements use individual `execute`
calls so DDL does not implicitly commit through `executescript`.

## Project layout

```text
src/zymptom/
    domain/        # Dataclass, enums, validation, timestamp conversion
    persistence/   # SQLite connection, migrations, atomic CRUD
    services/      # Small application-facing episode service
tests/
    domain/
    persistence/
```

Z0 stores data in a local SQLite file. Encryption and backup management are not
implemented in this foundation. Licensed under MIT; see `LICENSE`.
