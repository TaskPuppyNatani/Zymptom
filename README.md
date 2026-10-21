# Zymptom

Zymptom is a local-first functional journal. Its atomic record is a
`FunctionalEpisode`: one real-world attempt to perform an activity, including
duration, difficulties, adaptations, help, outcome, recovery, and ability to resume.

This repository implements **Z1 — First Usable Logger**, on the existing Z0
foundation. The PySide6 desktop app opens on Today and lets you record, reopen,
and edit functional attempts. The domain, SQLite persistence, and services remain
independent of Qt. History navigation and other Z2/Z3 features are not included.

## Install and test

Requires Python 3.12 or newer and PySide6. Domain and persistence use only the
standard library. On Linux, a desktop Qt platform plugin and its system libraries
are needed; see the [Qt for Python installation guide](https://doc.qt.io/qtforpython-6/gettingstarted.html).

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```

Launch the app:

```bash
zymptom
# Or: python -m zymptom
```

The default journal lives in Qt's per-user application-data directory (on Linux,
normally `~/.local/share/Zymptom/Zymptom/zymptom.sqlite3`). The app creates that
directory and applies pending migrations automatically. To use an existing or
specific journal:

```bash
python -m zymptom --database ./zymptom.sqlite3
```

An explicitly selected path must have an existing parent directory. Z0 database
initialization remains available without launching Qt:

```bash
python -m zymptom --init-db --database ./zymptom.sqlite3
```

## Daily logging

Today shows only the current local calendar day, oldest first. Click **+ Log
Something** (or Ctrl+N), enter what you attempted, choose an outcome, and Save.
Time defaults to now. Duration, quick adaptations/needs, and recovery duration
are optional. A basic entry does not require expanding More details.

**More details** exposes functional demands, symptoms and symptom notes, all
adaptations, help and help notes, consequences, recovery actions, recovery-required
status, resume ability, and notes. Tag controls offer common suggestions or accept
free text. Choose/type a value and click Add; Remove selected deletes a chosen
tag. Existing custom values, duplicates, and order are retained.

Quick choices share those tag collections: changed position, sat down, took break,
and needed help are adaptations; lay down, medication, and sensory recovery are
recovery actions. Medication is simply an action tag. Quick choices do not infer
a help level or recovery-required status; those remain optional detail fields.

Click a Today entry, or select it and press Enter, to edit it. Save refreshes Today
immediately. An entry moved to another local day leaves Today. Cancel or Escape
discards only the draft. Failed saves keep the form open for retry or cancellation.
Ctrl+S saves explicitly; Enter in a field does not submit the form. Controls use
the system font, accessible names, a logical Tab order, and visible checked states.

Displayed dates/times use the system timezone; persistence stays UTC-aware.
Unchanged timestamps retain their original microseconds and daylight-saving-time
instant. Changed timestamps use the local date/time control's timezone and Qt's
millisecond precision. Today refreshes periodically to handle local-day rollover.

UI integration tests use Qt's offscreen platform by default and QtTest, with the
same pytest fixtures and real temporary databases as the foundation tests:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/ui
```

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
    ui/            # Today, editor, Qt controls, local-time boundary, app lifetime
tests/
    domain/
    persistence/
    ui/
```

The journal is stored in a local SQLite file. Encryption and backup management
are not implemented. Licensed under MIT; see `LICENSE`.
