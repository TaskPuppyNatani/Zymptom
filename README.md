# Zymptom

Zymptom is a local-first functional journal. Its atomic record is a
`FunctionalEpisode`: one real-world attempt to perform an activity, including
duration, difficulties, adaptations, help, outcome, recovery, and ability to resume.

The PySide6 desktop app opens on Today and lets you record, reopen, edit, and
find functional attempts. It supports local voice dictation and JSON, CSV, and
human-readable Markdown exports. The domain, SQLite persistence, and services
remain independent of Qt.

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

To launch from the Linux application menu without activating the virtual
environment or changing directories, install the user-level desktop entry after
installing the project dependencies:

```bash
./scripts/install-desktop.sh
```

The installer targets this checkout's `.venv`, uses the packaged Zymptom
branding, and works without root access. It also saves the current Whisper
environment variables when present. Otherwise, on first setup, it detects the
common `~/Projects/whisper.cpp` executable and `ggml-small.en.bin` model paths
when both files exist. After `git pull` and any needed dependency updates, the
same application-menu entry launches the updated checkout. Remove the launcher
with `./scripts/uninstall-desktop.sh`; this leaves dictation settings and the
journal database untouched.

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

## Local backups and restore

Open **Journal → Backups / Restore…** to create a backup immediately or select a
verified backup to restore. The list shows local creation time, backup type, size,
schema version, and validation status. Backup files are complete SQLite journals.
They live in `backups/` beside the resolved journal file: normally
`~/.local/share/Zymptom/Zymptom/backups/`, using the existing Qt/XDG data location.
An explicit `--database` path uses its own sibling backup directory.

Zymptom checks at startup, once a minute while open, and when an editor closes.
It creates at most one daily snapshot per local calendar day, and skips later
days until the journal differs from the previous successful daily snapshot.
Existing file-backed journals are backed up before pending migrations; a required
backup failure prevents the upgrade. Fresh database initialization has no prior
journal to back up. The newest 30 verified daily/migration snapshots are retained.
Manual and safety/pre-restore backups are kept independently and are never
automatically pruned. A retention failure leaves the newly verified backup usable.

Snapshots use the [SQLite backup API](https://www.sqlite.org/backup.html), including
committed data in WAL files. Each snapshot is integrity-checked, checked for child
record consistency, and checked against supported migration history and journal
fields before it is published. Small JSON sidecars contain timestamps, identifiers,
sizes, versions, validation results, and hashes, without journal text. If a sidecar
is lost or damaged, the catalog reconstructs those facts from the filename and
the validated snapshot. Snapshots and metadata are created with user-only file
permissions and placed atomically after validation.

Restore first validates and stages the selection, upgrading the staged copy only
through supported migrations. It then creates a fresh verified safety backup and
asks for explicit confirmation, with Cancel as the default. Finish any editor or
export dialog before opening the backup surface. During final restoration, SQLite
locks the journal, checks that it still matches the safety backup, and replaces
its contents transactionally; Zymptom validates the result and reconnects its
services. Today and History refresh while retaining History's filters. Corrupt,
incompatible, or newer-schema snapshots are refused. A failed restore recovers
the previous journal where storage remains usable, and always retains the safety
backup; an unrecoverable storage/reopen failure reports its exact recovery path
and closes the database connection.

## Local sync contract and simulator

The separate [PWA M0 mobile proof](pwa/README.md) uses disposable browser-local
data only. It does not sync with or open the desktop journal.

Z5.0 maintains installation identity, immutable episode revisions, tombstones,
provider-scoped acknowledgements, and explicit concurrent-edit conflicts locally.
It has no network transport or account requirements. Normal offline logging is
unchanged. The [sync contract v1](docs/sync-contract-v1.md) documents the JSON format,
merge rules, migration, safety-backup requirement, and explicit resolution API.

Run the two-device demonstration with `.venv/bin/python -m zymptom.sync.simulator`.
It uses temporary stores only and reports convergence/conflict counts without
printing journal contents. No synchronization or conflict-resolution UI is exposed.

## Daily logging

Today shows only the current local calendar day, oldest first. Click **+ Log
Something** (or Ctrl+N), enter what you attempted, choose an outcome, and Save.
Time defaults to now. Duration, quick adaptations/needs, and recovery duration
are optional. A basic entry does not require expanding More details.

**More details** exposes functional demands, symptoms and symptom notes, all
adaptations, help and help notes, consequences, recovery actions, recovery-required
status, resume ability, and notes. Tag controls offer common suggestions or accept
free text. Check multiple suggestions, close the chooser, and click Add once;
values are appended in selection order. Repeated Add attempts skip values already
present. Remove selected deletes a chosen tag, which can then be added again at
the end. Existing custom values and list order are retained. Older duplicate rows
load faithfully and are reduced to their first occurrence only when saving.

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

## Delete an entry

Open a saved episode from Today or History, then choose **Delete entry** on the
left of the editor's action row. The confirmation shows the saved activity and
local date/time. **Cancel** is the default; only explicitly choosing **Delete
entry** permanently removes that episode and its related tags. Unsaved edits are
discarded. New unsaved episodes have no Delete action.

Successful deletion closes the editor and immediately refreshes Today and History
while retaining History's active filters. If deletion fails, the editor stays
open with the draft intact and an error, allowing retry or cancellation.

## Local voice dictation

In the episode editor, click the microphone button beside the text field you
want to dictate into. Activity, symptom notes, help notes, and general notes are
supported. Expand **More details** to reach the notes fields and their microphone
buttons. The existing editor has no separate recovery-notes field.
The dialog shows **Listening…** while recording. Choose **Stop** to transcribe,
review and edit the result, then explicitly choose **Insert text**. Inserting
appends to existing text (a space for activity, a new line for notes) and never
saves the episode. Choose the editor's Save separately. Cancel, Escape, or closing
the dialog discards dictation and leaves the destination unchanged.

Dictation uses Qt Multimedia's default desktop microphone and a separately
installed local [whisper.cpp CLI](https://github.com/ggml-org/whisper.cpp/tree/master/examples/cli).
Provide an existing whisper.cpp-compatible executable and model file; Zymptom
does not install engines, download models, or use cloud transcription. The
desktop installer detects the common local `~/Projects/whisper.cpp` paths above.
For a different setup, save settings in `~/.config/zymptom/config.json` (or
`$XDG_CONFIG_HOME/zymptom/config.json`):

```json
{
  "whisper_command": "/absolute/path/to/whisper-cli",
  "whisper_model": "/absolute/path/to/ggml-base.en.bin"
}
```

`ZYMPTOM_WHISPER_COMMAND` and `ZYMPTOM_WHISPER_MODEL` remain available and
override their saved values for development or testing. The command is one
trusted local whisper.cpp-compatible executable path or command name, without
shell syntax or extra arguments. The adapter passes local model and WAV paths
directly to that process and requests a UTF-8 text file. Other speech engines
can implement the small `AudioCapture` / `LocalTranscriber` ports without
changing the editor or episode schema.

Linux is the primary capture platform. Qt Multimedia needs a working system
audio service, a default microphone, and input permission. The device must support
16 kHz mono, 16-bit PCM capture. Missing engines/models, unavailable microphones,
and recording/transcription errors are reported in the dialog; typed logging
remains available. Recording stops after two minutes; transcription times out
after five minutes. Cancel also stops transcription.

Audio and intermediate transcript files live only in a private temporary session
directory. They are removed before transcript review and on failure, cancellation,
editor close, and normal application shutdown. Audio never enters the journal.
An abrupt process termination or power loss can prevent normal cleanup; this is
not an audio history feature. Automated dictation tests use fake audio devices
and local test processes, requiring neither a microphone nor Whisper.

## History and search

Choose **History** beside Today to review persisted episodes, newest first,
grouped by local calendar date. Click an episode or select it and press Enter to
open the same editor used by Today. Saving refreshes both views and retains the
active History filters. An entry that no longer matches those filters disappears
from the results.

Search matches activity text only, using a case-insensitive literal substring
(including Unicode). Blank search applies no activity restriction. Outcome and
functional-demand filters combine with search and dates using AND. Demand choices
come from actual stored tag values, including custom values; matching uses the
exact tag, rather than its display label.

Enable **From** and/or **Through** to apply an inclusive local date range. The date
fields initially offer the last 30 calendar days, but are inactive until checked.
Use **Clear filters** to return to all entries. Local calendar bounds convert to
UTC at the UI boundary and account for 23- and 25-hour daylight-saving days;
timestamp storage is unchanged. History queries use the existing SQLite schema,
so this release requires no new migration.

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
- Five child tables store tags with explicit list positions. Writes keep unique
  values in first-occurrence order; reads preserve any older duplicate rows.
  Foreign keys are enabled on every factory connection,
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

`src/zymptom/persistence/migrations.py` defines the ordered, append-only
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
    persistence/   # SQLite connection, migrations, atomic CRUD, validated backups
    services/      # Episode API and journal/restore lifetime
    sync/          # Local revision contract, reconciliation, device identity, simulator
    dictation/     # Temporary audio capture, local STT port/adapter, session
    ui/            # Today, editor, Qt controls, local-time boundary, app lifetime
tests/
    domain/
    persistence/
    ui/
```

The journal is stored in a local SQLite file. Encryption is not implemented.
Licensed under MIT; see `LICENSE`.
