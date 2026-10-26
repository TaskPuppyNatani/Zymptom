# Sync contract v1

Z5.0 defines local record reconciliation. It supplies no transport, account,
provider SDK, network calls, or synchronization UI. Run the disposable developer
demonstration with:

```bash
.venv/bin/python -m zymptom.sync.simulator
```

The utility creates two temporary installations and journals, exchanges JSON
changes, reports convergence/conflict counts, and removes those stores on exit.
It never opens the normal journal or prints episode contents. `LocalSimulator`
and `DeviceStore` also expose their episode/sync services for deterministic tests.

## Identity and storage

`DeviceIdentity.get()` creates an installation UUID once in
`$XDG_CONFIG_HOME/zymptom/device.json`, falling back to
`~/.config/zymptom/device.json`. Atomic placement and a local file lock prevent
competing processes from choosing different identities. The file is user-only
and contains only a format version and UUID. No hostname or account is required.
An invalid existing file fails explicitly. `DeviceIdentity.reset()` intentionally
replaces it; restart the app to adopt that identity. Historical revision origins
are never rewritten. Identity lives outside SQLite, so restoring a journal does
not clone or rewind the installation identity. Repository/session constructors
accept an explicit device ID for isolated devices and tests.

SQLite migration **2**, `sync_contract_v1`, adds:

| Table | Purpose |
| --- | --- |
| `sync_changes` | Immutable canonical revision JSON, indexed by record and generation; also the durable delivery receipt/history |
| `sync_heads` | The currently visible revision for each record, including tombstones |
| `sync_conflicts` | Both immutable candidates, common base when known, and optional explicit resolution revision |
| `sync_acknowledgements` | Change IDs acknowledged under each opaque provider namespace |

The migration runs through the existing pre-migration backup hook. It backfills
existing episodes with deterministic baseline roots inside the migration
transaction. Existing episode fields, timestamps, list order, and legacy duplicate
tags remain untouched. Identical old snapshots produce identical baseline IDs.
These roots use the reserved all-zero origin UUID; they do not invent a device
that authored historical data. All subsequent local changes use the installation
identity. Tombstones and revision history have no foreign key to the live episode
row and survive its cascading deletion. No history/tombstone pruning is provided.

## Wire representation

`SyncBatch.to_json()` produces UTF-8-compatible JSON with exactly these top-level
fields:

```json
{"contract_version":1,"record_schema_version":1,"changes":[]}
```

Each change contains exactly:

| Field | Meaning |
| --- | --- |
| `contract_version` | Integer `1` |
| `record_schema_version` | Integer `1`, independent of SQLite's migration version |
| `record_type` | `functional_episode` |
| `change_id` | Immutable revision UUID |
| `record_id` | Stable episode UUID |
| `revision` | Positive integer generation: one plus the greatest parent generation, or 1 for a root |
| `parent_ids` | Unique parent revision UUIDs, serialized in UUID order |
| `operation` | `create`, `update`, `delete`, or `resolve` |
| `updated_at` | Aware timestamp of the meaningful revision, serialized in UTC with six fractional digits |
| `device_id` | Origin installation UUID, retained unchanged on receipt |
| `deleted` | Boolean tombstone indicator |
| `content_hash` | SHA-256 of canonical meaningful record content |
| `episode` | Complete episode snapshot, or `null` for a tombstone |

Active snapshots require every `FunctionalEpisode` field, including explicit
`null` values. UUIDs/enums are machine strings; timestamps are aware ISO strings.
The five multi-value fields remain ordered JSON arrays. Unicode, multiline text,
empty text, null, zero, and false retain their distinct meanings. Snapshots are
immutable strings internally; reading a candidate episode produces a detached
object. Ordinary later saves still perform the existing ordered deduplication.

Canonical JSON uses sorted keys, compact separators, literal Unicode, and no
non-finite numbers or duplicate object keys. Changes are ordered by generation,
record UUID, then revision UUID. IDs and timestamps normalize to canonical UUIDs
and UTC. Unknown fields, malformed snapshots, inconsistent hashes, ID reuse with
different contents, invalid ancestry, and unsupported versions are rejected.
Only version 1 is supported; version 0 and future versions have no implicit
upgrade path. This format is separate from the user-facing JSON export.

The content hash includes all episode fields except `updated_at`. The existing
local editor may advance its audit timestamp on an unchanged save; that does not
create a new sync revision. The wire snapshot retains the meaningful revision's
timestamp. Provider acknowledgements also do not create revisions. Timestamp
ordering is never used to decide ancestry or choose a conflicting edit.

## Revision and merge rules

Ordinary local creates, meaningful edits, and deletes append immutable revisions
in the **same transaction** as the episode write. Failed local writes roll back
both. A delete removes the normal episode/child rows and appends a tombstone
whose parent is the previous active revision. An ordinary create cannot reuse a
tombstoned ID; ordinary updates cannot descend from a tombstone.

Every parent must already be known or arrive in the same batch. Parents belong
to the same record; the generation must match them. Missing history is refused
so a future provider must supply the ancestors rather than asking Zymptom to
guess causality. Input batch order is immaterial.

| Situation | Result |
| --- | --- |
| Independent creates, different record IDs | Keep both |
| One-sided descendant update | Apply the descendant, even if its clock is older |
| Exact repeated delivery/batch | No record, revision, or conflict churn |
| Delete versus unchanged ancestor | Keep the descendant tombstone |
| Old active ancestor after delete | Keep the tombstone; no resurrection |
| Concurrent edit/edit | Preserve local visibility and both candidates; explicit conflict |
| Concurrent delete/edit | Keep the locally visible active/deleted state and preserve both candidates; explicit conflict |
| Independent creates reusing a record UUID | Explicit conflict with no invented common base |
| Concurrent identical edits or concurrent deletes | Explicit conflict as well; equal content does not prove common revision identity |

Each conflict has a deterministic ID from its unordered candidate pair. It
preserves the local/incoming revisions and a common ancestor, chosen by greatest
generation with UUID tie-breaking if there is more than one. Complete ancestry
remains available in revision history. Conflict inspection also returns the
current head and all current branch tips. Later incoming descendants preserve
earlier conflict details and add the current competing candidate when needed.

Ordinary local edit/delete is refused for a record with unresolved conflicts.
This developer-only state cannot occur through normal offline logging alone.
`unresolved_conflicts()` and `inspect_conflict(id)` expose it without a resolution
UI. `resolve(record_id, chosen_change_id=...)` requires an explicit **current tip**
choice. It creates a new revision whose parents include all current tips,
preserves the chosen contents (or tombstone), and closes covered conflicts.
Superseded candidates cannot silently overwrite later edits. Resolution is backed
up and transactional too. Receiving that resolution converges descendants while
retaining both original candidates for inspection. Repeated resolution is refused.

## Merge transactions and safety

Construct `SyncService(episodes, before_merge=session.before_destructive_change)`
using the existing `JournalSession` safety hook. A non-empty incoming batch is
fully parsed and checked before mutation. It then requires a newly validated
Z4.3 safety snapshot matching the current journal. After acquiring its write
transaction, the service checks that fingerprint again and revalidates against
the current heads. An intervening writer causes a safe retry. The entire batch,
including conflicts and provider receipts, commits atomically or rolls back.
Backup failure aborts the merge. A failed batch leaves its safety snapshot intact.

Exact duplicate non-empty deliveries still require that safety hook, but do not
change journal/sync state. Empty batches are no-ops without a backup. The merge
applies record snapshots through the repository; it never replaces or copies the
live SQLite database file. Backup validation now checks sync schema/history,
graph integrity, visible heads, conflicts, and provider acknowledgements as well
as the existing journal checks. Older supported database snapshots gain baseline
metadata through the normal migration path when explicitly restored.

## Provider-neutral checkpoints

`pending(provider)` exports all unacknowledged immutable changes, including the
history needed to establish ancestry. `acknowledge(provider, change_ids)` marks
only known change IDs, atomically, after a future provider has durably accepted
them. The caller is responsible for that acceptance; calling acknowledge does not
perform delivery. Namespaces are opaque local strings, not URLs or accounts.

`apply(batch, source_provider=...)` records that source as already knowing the
received changes, preventing echoes to that same provider. Other namespaces have
independent checkpoints. `known_revision(record_id)` returns the visible head,
including a tombstone. All history, conflicts, and acknowledgements survive
restart. These are local service contracts only; no background jobs or transport
integration are enabled.
