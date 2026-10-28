import { type Episode, type EpisodeInput, nowUtc, tagFields, unique, utc, uuidPattern, validateEpisode } from './model';
import { parseCanonicalChange, type Z5Change } from './sandbox/z5-wire';
import { buildChange, frontier, reconcile, validateLegacyOrigin, type SyncConflict } from './sync';

export const DATABASE_NAME = 'zymptom-pwa-m0-disposable';
export const PRODUCTION_DATABASE_NAME = 'zymptom-pwa-production';
export interface LocalRecord {
  record_type: 'functional_episode';
  record_id: string;
  local_revision: number;
  local_change_id: string;
  previous_local_change_id: string | null;
  device_id: string;
  updated_at: string;
  deleted: boolean;
  episode: Episode | null;
}
interface Snapshot { records: LocalRecord[]; changes: Z5Change[]; conflicts: SyncConflict[]; version: number; }

export class EpisodeStore {
  private constructor(private database: IDBDatabase) {}

  static async open(name = DATABASE_NAME): Promise<EpisodeStore> {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(name, 2);
      let blocked = false;
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains('episodes')) request.result.createObjectStore('episodes', { keyPath: 'record_id' });
        if (!request.result.objectStoreNames.contains('installation')) request.result.createObjectStore('installation');
        request.result.createObjectStore('changes', { keyPath: 'change_id' });
        request.result.createObjectStore('conflicts', { keyPath: 'id' });
        request.result.createObjectStore('acknowledgements');
      };
      request.onerror = () => reject(new Error('Browser storage could not be opened. Check browser storage permissions.'));
      request.onblocked = () => {
        blocked = true;
        reject(new Error('Close other Zymptom proof tabs and reopen this page.'));
      };
      request.onsuccess = () => { if (blocked) request.result.close(); else resolve(request.result); };
    });
    database.onversionchange = () => database.close();
    const store = new EpisodeStore(database);
    try { await store.bootstrap(); } catch (failure) { store.close(); throw failure; }
    return store;
  }

  close(): void { this.database.close(); }

  private run<T>(stores: string[], mode: IDBTransactionMode, action: (transaction: IDBTransaction, set: (value: T) => void, guard: (callback: () => void) => void) => void): Promise<T> {
    return new Promise((resolve, reject) => {
      const transaction = this.database.transaction(stores, mode);
      let result: T;
      let failure: unknown;
      const protect = (callback: () => void) => {
        try { callback(); } catch (error) { failure = error; transaction.abort(); }
      };
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => { failure ??= transaction.error; };
      transaction.onabort = () => reject(failure ?? new Error('The local change could not be saved. Your entry is unchanged.'));
      protect(() => action(transaction, (value) => { result = value; }, protect));
    });
  }

  async deviceId(): Promise<string> {
    return this.run(['installation'], 'readwrite', (transaction, set, guard) => {
      const store = transaction.objectStore('installation');
      const request = store.get('device_id');
      request.onsuccess = () => guard(() => {
        const identity: unknown = request.result ?? crypto.randomUUID();
        if (typeof identity !== 'string' || !uuidPattern.test(identity) || identity === '00000000-0000-0000-0000-000000000000') throw new Error('Invalid local installation identity.');
        if (request.result === undefined) store.put(identity, 'device_id');
        set(identity);
      });
    });
  }

  async journalId(): Promise<string | null> {
    return this.run(['installation'], 'readonly', (transaction, set, guard) => {
      const request = transaction.objectStore('installation').get('journal_id');
      request.onsuccess = () => guard(() => {
        const identity: unknown = request.result;
        if (identity === undefined || identity === null) { set(null); return; }
        if (typeof identity !== 'string' || !uuidPattern.test(identity) || identity !== identity.toLowerCase() || identity === '00000000-0000-0000-0000-000000000000') {
          throw new Error('Invalid local production journal identity.');
        }
        set(identity);
      });
    });
  }

  async lastSyncedAt(): Promise<string | null> {
    return this.run(['installation'], 'readonly', (transaction, set) => {
      const request = transaction.objectStore('installation').get('last_synced_at');
      request.onsuccess = () => set(typeof request.result === 'string' ? request.result : null);
    });
  }

  async setLastSyncedAt(isoTimestamp: string): Promise<void> {
    await this.run<void>(['installation'], 'readwrite', (transaction, set) => {
      transaction.objectStore('installation').put(isoTimestamp, 'last_synced_at');
      set(undefined);
    });
  }

  private async snapshot(): Promise<Snapshot> {
    return this.run(['episodes', 'changes', 'conflicts', 'installation'], 'readonly', (tx, set, guard) => {
      const records = tx.objectStore('episodes').getAll();
      const changes = tx.objectStore('changes').getAll();
      const conflicts = tx.objectStore('conflicts').getAll();
      const version = tx.objectStore('installation').get('sync_version');
      version.onsuccess = () => guard(() => set({ records: records.result as LocalRecord[], changes: changes.result as Z5Change[],
        conflicts: conflicts.result as SyncConflict[], version: (version.result as number | undefined) ?? 0 }));
    });
  }

  private async commit(snapshot: Snapshot, changes: Z5Change[], records: LocalRecord[], conflicts: SyncConflict[], provider?: string, acknowledged: string[] = [], journalId?: string): Promise<void> {
    await this.run<void>(['episodes', 'changes', 'conflicts', 'installation', 'acknowledgements'], 'readwrite', (tx, set, guard) => {
      const installation = tx.objectStore('installation');
      const version = installation.get('sync_version');
      version.onsuccess = () => guard(() => {
        if (((version.result as number | undefined) ?? 0) !== snapshot.version) throw new Error('Local entries changed in another tab. Please retry.');
        for (const change of changes) tx.objectStore('changes').add(change);
        for (const record of records) tx.objectStore('episodes').put(record);
        for (const conflict of conflicts) tx.objectStore('conflicts').put(conflict);
        if (provider) for (const id of acknowledged) tx.objectStore('acknowledgements').put(true, `${provider}:${id}`);
        if (journalId) installation.put(journalId, 'journal_id');
        installation.put(snapshot.version + 1, 'sync_version');
        set(undefined);
      });
    });
  }

  private async bootstrap(): Promise<void> {
    const device = await this.deviceId();
    const snapshot = await this.snapshot();
    if (snapshot.version) return;
    const changes: Z5Change[] = [];
    const records: LocalRecord[] = [];
    for (const record of snapshot.records) {
      if (record.deleted) continue; // M0 retained no snapshot, so keep this reserved tombstone locally.
      validateEpisode(record.episode);
      const change = await buildChange(record.record_id, record.episode, device, 'create', [], record.local_change_id);
      changes.push(change);
      records.push(this.head(change));
    }
    try { await this.commit(snapshot, changes, records, []); }
    catch (failure) {
      // Another tab may have completed the same one-time bootstrap.
      if (!(await this.snapshot()).version) throw failure;
    }
  }

  private head(change: Z5Change): LocalRecord {
    return { record_type: 'functional_episode', record_id: change.record_id, local_revision: change.revision,
      local_change_id: change.change_id, previous_local_change_id: change.parent_ids[0] ?? null,
      device_id: change.device_id, updated_at: change.updated_at, deleted: change.deleted, episode: change.episode };
  }

  async history(): Promise<Z5Change[]> {
    return (await this.snapshot()).changes.sort((a, b) => a.revision - b.revision || a.record_id.localeCompare(b.record_id) || a.change_id.localeCompare(b.change_id));
  }

  async conflicts(): Promise<Array<{ conflict: SyncConflict; candidates: Z5Change[] }>> {
    const snapshot = await this.snapshot();
    const changes = new Map(snapshot.changes.map((change) => [change.change_id, change]));
    return snapshot.conflicts.filter((conflict) => !conflict.resolution).map((conflict) => ({ conflict, candidates: frontier(conflict.recordId, changes) }));
  }

  async pending(provider: string): Promise<Z5Change[]> {
    return this.run(['changes', 'acknowledgements'], 'readonly', (tx, set, guard) => {
      const changes = tx.objectStore('changes').getAll();
      const keys = tx.objectStore('acknowledgements').getAllKeys();
      keys.onsuccess = () => guard(() => {
        const acknowledged = new Set(keys.result.map(String));
        set((changes.result as Z5Change[]).filter((change) => !acknowledged.has(`${provider}:${change.change_id}`))
          .sort((a, b) => a.revision - b.revision || a.change_id.localeCompare(b.change_id)));
      });
    });
  }

  async acknowledge(provider: string, id: string): Promise<void> {
    await this.run<void>(['changes', 'acknowledgements'], 'readwrite', (tx, set, guard) => {
      const request = tx.objectStore('changes').get(id);
      request.onsuccess = () => guard(() => {
        if (!request.result) throw new Error('Cannot acknowledge an unknown change.');
        tx.objectStore('acknowledgements').put(true, `${provider}:${id}`);
        set(undefined);
      });
    });
  }

  async apply(payloads: string[], provider: string): Promise<void> {
    const incoming = await Promise.all(payloads.map(parseCanonicalChange));
    await Promise.all(incoming.map(validateLegacyOrigin));
    if (new Set(incoming.map((change) => change.change_id)).size !== incoming.length) throw new Error('Duplicate incoming change identity.');
    const snapshot = await this.snapshot();
    const changes = new Map(snapshot.changes.map((change) => [change.change_id, change]));
    const heads = new Map(snapshot.records.filter((record) => changes.has(record.local_change_id)).map((record) => [record.record_id, record.local_change_id]));
    const reserved = new Set(snapshot.records.filter((record) => record.deleted && !changes.has(record.local_change_id)).map((record) => record.record_id));
    if (incoming.some((change) => reserved.has(change.record_id))) throw new Error('An incoming record collides with a preserved M0 tombstone.');
    const original = new Set(changes.keys());
    const conflicts = structuredClone(snapshot.conflicts);
    await reconcile(changes, heads, conflicts, incoming);
    const records = [...heads.values()].map((id) => this.head(changes.get(id)!));
    await this.commit(snapshot, [...changes.values()].filter((change) => !original.has(change.change_id)), records, conflicts,
      provider, incoming.map((change) => change.change_id));
  }

  async joinProductionJournal(journalId: string, payloads: string[], provider: string): Promise<void> {
    if (typeof journalId !== 'string' || !uuidPattern.test(journalId) || journalId !== journalId.toLowerCase() || journalId === '00000000-0000-0000-0000-000000000000') {
      throw new Error('Invalid production journal identity.');
    }
    if (provider !== `firebase-production:${journalId}`) {
      throw new Error('Invalid production sync provider.');
    }
    const incoming = await Promise.all(payloads.map(parseCanonicalChange));
    await Promise.all(incoming.map(validateLegacyOrigin));
    if (new Set(incoming.map((change) => change.change_id)).size !== incoming.length) throw new Error('Duplicate incoming change identity.');
    const existingJournal = await this.journalId();
    const snapshot = await this.snapshot();
    if (existingJournal !== null && existingJournal !== journalId) {
      throw new Error('This browser is already linked to a different production journal. Local production data was not changed.');
    }
    if (existingJournal === null && (snapshot.records.length > 0 || snapshot.changes.length > 0 || snapshot.conflicts.length > 0)) {
      throw new Error('Local production storage already contains unlinked journal data. Refusing to merge automatically.');
    }
    const changes = new Map(snapshot.changes.map((change) => [change.change_id, change]));
    const heads = new Map(snapshot.records.filter((record) => changes.has(record.local_change_id)).map((record) => [record.record_id, record.local_change_id]));
    const original = new Set(changes.keys());
    const conflicts = structuredClone(snapshot.conflicts);
    await reconcile(changes, heads, conflicts, incoming);
    const records = [...heads.values()].map((id) => this.head(changes.get(id)!));
    await this.commit(snapshot, [...changes.values()].filter((change) => !original.has(change.change_id)), records, conflicts,
      provider, incoming.map((change) => change.change_id), journalId);
  }

  async resolve(recordId: string, selectedId: string): Promise<void> {
    const snapshot = await this.snapshot();
    const changes = new Map(snapshot.changes.map((change) => [change.change_id, change]));
    const candidates = frontier(recordId, changes);
    const chosen = candidates.find((change) => change.change_id === selectedId);
    if (!chosen || candidates.length < 2 || !snapshot.conflicts.some((conflict) => conflict.recordId === recordId && !conflict.resolution)) throw new Error('Inspect the current conflict and choose a current version.');
    let time = nowUtc();
    const newest = candidates.map((candidate) => candidate.updated_at).sort().at(-1)!;
    if (time <= newest) {
      const micros = Number(newest.slice(20, 26));
      time = micros < 999999 ? `${newest.slice(0, 20)}${String(micros + 1).padStart(6, '0')}+00:00` : utc(new Date(new Date(newest).getTime() + 1).toISOString());
    }
    const episode = chosen.episode ? { ...chosen.episode, updated_at: time } : null;
    const resolution = await buildChange(recordId, episode, await this.deviceId(), 'resolve', candidates, undefined, time);
    const heads = new Map(snapshot.records.filter((record) => changes.has(record.local_change_id)).map((record) => [record.record_id, record.local_change_id]));
    const conflicts = structuredClone(snapshot.conflicts);
    await reconcile(changes, heads, conflicts, [resolution]);
    await this.commit(snapshot, [resolution], [this.head(resolution)], conflicts);
  }

  async list(): Promise<LocalRecord[]> {
    return this.run(['episodes'], 'readonly', (transaction, set, guard) => {
      const request = transaction.objectStore('episodes').getAll();
      request.onsuccess = () => guard(() => {
        const records = request.result as LocalRecord[];
        for (const record of records) if (!record.deleted) validateEpisode(record.episode);
        set(records.filter((record) => !record.deleted).sort((a, b) => a.episode!.occurred_at.localeCompare(b.episode!.occurred_at) || a.record_id.localeCompare(b.record_id)));
      });
    });
  }

  async get(id: string): Promise<LocalRecord | undefined> {
    return this.run(['episodes'], 'readonly', (transaction, set) => {
      const request = transaction.objectStore('episodes').get(id);
      request.onsuccess = () => set(request.result);
    });
  }

  async create(input: EpisodeInput): Promise<LocalRecord> {
    const time = nowUtc();
    return this.write({ ...structuredClone(input), id: crypto.randomUUID(), created_at: time, updated_at: time }, null, false);
  }

  async update(episode: Episode, expectedRevision: number, expectedChangeId?: string): Promise<LocalRecord> {
    return this.write(episode, expectedRevision, false, expectedChangeId);
  }

  async delete(id: string, expectedRevision: number, expectedChangeId?: string): Promise<LocalRecord> {
    const record = await this.get(id);
    if (!record?.episode) throw new Error('This entry is already deleted.');
    return this.write(record.episode, expectedRevision, true, expectedChangeId);
  }

  private async write(value: Episode, expected: number | null, deleted: boolean, expectedChangeId?: string): Promise<LocalRecord> {
    const episode = structuredClone(value);
    for (const field of ['occurred_at', 'created_at', 'updated_at'] as const) episode[field] = utc(episode[field]);
    for (const field of tagFields) episode[field] = unique(episode[field]);
    validateEpisode(episode);
    const device = await this.deviceId();
    const snapshot = await this.snapshot();
    const previous = snapshot.records.find((record) => record.record_id === episode.id);
        if (expected === null ? previous !== undefined : !previous || previous.deleted || previous.local_revision !== expected || (expectedChangeId !== undefined && previous.local_change_id !== expectedChangeId)) {
          throw new Error('This entry changed in another tab. Cancel and reopen it before saving.');
        }
        if (snapshot.conflicts.some((conflict) => conflict.recordId === episode.id && !conflict.resolution)) throw new Error('This entry has an unresolved sync conflict. Resolve it before editing or deleting.');
        if (previous) {
          episode.created_at = previous.episode!.created_at;
          episode.updated_at = nowUtc();
          if (episode.updated_at <= previous.updated_at) {
            episode.updated_at = new Date(new Date(previous.updated_at).getTime() + 1).toISOString().replace(/(\d{3})Z$/, '$1000+00:00');
          }
        }
    const parent = previous ? snapshot.changes.find((change) => change.change_id === previous.local_change_id) : undefined;
    if (previous && !parent) throw new Error('Missing local change history.');
    const change = await buildChange(episode.id, deleted ? null : episode, device, deleted ? 'delete' : previous ? 'update' : 'create', parent ? [parent] : [], undefined, episode.updated_at);
    if (parent && !deleted && parent.content_hash === change.content_hash) return previous!;
    const record = this.head(change);
    await this.commit(snapshot, [change], [record], []);
    return record;
  }

  // Developer-only helper. No production journal, exports, or other origins.
  async resetDisposableData(): Promise<void> {
    await this.run<void>(['episodes', 'changes', 'conflicts', 'acknowledgements', 'installation'], 'readwrite', (transaction, set) => {
      transaction.objectStore('episodes').clear();
      transaction.objectStore('changes').clear();
      transaction.objectStore('conflicts').clear();
      transaction.objectStore('acknowledgements').clear();
      const version = transaction.objectStore('installation').get('sync_version');
      version.onsuccess = () => transaction.objectStore('installation').put(((version.result as number | undefined) ?? 0) + 1, 'sync_version');
      set(undefined);
    });
  }
}
