import { type Episode, type EpisodeInput, nowUtc, tagFields, unique, utc, uuidPattern, validateEpisode } from './model';

export const DATABASE_NAME = 'zymptom-pwa-m0-disposable';
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
// Deliberately not a Z5 Change: no transport, hashes, complete revision DAG, or
// merge/acknowledgement state is claimed by these local-only head records.
export class EpisodeStore {
  private constructor(private database: IDBDatabase) {}

  static async open(name = DATABASE_NAME): Promise<EpisodeStore> {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      let blocked = false;
      request.onupgradeneeded = () => {
        request.result.createObjectStore('episodes', { keyPath: 'record_id' });
        request.result.createObjectStore('installation');
      };
      request.onerror = () => reject(new Error('Browser storage could not be opened. Check browser storage permissions.'));
      request.onblocked = () => {
        blocked = true;
        reject(new Error('Close other Zymptom proof tabs and reopen this page.'));
      };
      request.onsuccess = () => { if (blocked) request.result.close(); else resolve(request.result); };
    });
    database.onversionchange = () => database.close();
    return new EpisodeStore(database);
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

  async update(episode: Episode, expectedRevision: number): Promise<LocalRecord> {
    return this.write(episode, expectedRevision, false);
  }

  async delete(id: string, expectedRevision: number): Promise<LocalRecord> {
    const record = await this.get(id);
    if (!record?.episode) throw new Error('This entry is already deleted.');
    return this.write(record.episode, expectedRevision, true);
  }

  private async write(value: Episode, expected: number | null, deleted: boolean): Promise<LocalRecord> {
    const episode = structuredClone(value);
    for (const field of ['occurred_at', 'created_at', 'updated_at'] as const) episode[field] = utc(episode[field]);
    for (const field of tagFields) episode[field] = unique(episode[field]);
    validateEpisode(episode);
    const device = await this.deviceId();
    return this.run(['episodes'], 'readwrite', (transaction, set, guard) => {
      const store = transaction.objectStore('episodes');
      const request = store.get(episode.id);
      request.onsuccess = () => guard(() => {
        const previous = request.result as LocalRecord | undefined;
        if (expected === null ? previous !== undefined : !previous || previous.deleted || previous.local_revision !== expected) {
          throw new Error('This entry changed in another tab. Cancel and reopen it before saving.');
        }
        if (previous) {
          episode.created_at = previous.episode!.created_at;
          episode.updated_at = nowUtc();
          if (episode.updated_at <= previous.updated_at) {
            episode.updated_at = new Date(new Date(previous.updated_at).getTime() + 1).toISOString().replace(/(\d{3})Z$/, '$1000+00:00');
          }
        }
        const record: LocalRecord = {
          record_type: 'functional_episode', record_id: episode.id,
          local_revision: (previous?.local_revision ?? 0) + 1,
          local_change_id: crypto.randomUUID(), previous_local_change_id: previous?.local_change_id ?? null,
          device_id: device, updated_at: episode.updated_at, deleted, episode: deleted ? null : episode,
        };
        store.put(record);
        set(record);
      });
    });
  }

  // Developer-only helper. No production journal, exports, or other origins.
  async resetDisposableData(): Promise<void> {
    await this.run<void>(['episodes'], 'readwrite', (transaction, set) => {
      transaction.objectStore('episodes').clear();
      set(undefined);
    });
  }
}
