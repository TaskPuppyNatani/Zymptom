import 'fake-indexeddb/auto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { blankEpisode, nowUtc, type EpisodeInput } from '../../src/model';
import { EpisodeStore, type LocalRecord } from '../../src/storage';
import { canonicalJson, parseCanonicalChange } from '../../src/sandbox/z5-wire';
import { syncDisposableEntries } from '../../src/sandbox/disposable-sync';
import { reconcile, type SyncConflict } from '../../src/sync';
import fixture from '../fixtures/z5-v1.json';

const stores: EpisodeStore[] = [];
async function open(name = crypto.randomUUID()) { const store = await EpisodeStore.open(name); stores.push(store); return store; }
afterEach(() => { for (const store of stores.splice(0)) store.close(); });
function input(activity: string): EpisodeInput { return { ...blankEpisode(), activity, outcome: 'stopped_early', duration_minutes: 0, recovery_required: false, notes: 'Line one\n雪', symptoms: ['pain', 'sensory_overload'] }; }
function mailbox() {
  const changes = new Map<string, string>();
  return {
    changes,
    async uploadCanonicalChange(payload: string) {
      const change = await parseCanonicalChange(payload);
      const old = changes.get(change.change_id);
      if (old && old !== payload) throw new Error('collision');
      changes.set(change.change_id, payload);
    },
    async pullChanges() { return [...changes.values()].map((canonicalPayload) => ({ canonicalPayload })); },
  };
}
const provider = 'test:disposable';
async function sync(store: EpisodeStore, box: ReturnType<typeof mailbox>) { await syncDisposableEntries(store, provider, box); }

describe('disposable Z5 history and reconciliation', () => {
  it('bootstraps M0 heads once without resurrecting tombstones', async () => {
    const name = crypto.randomUUID();
    const time = nowUtc();
    const id = crypto.randomUUID();
    const tombstone = crypto.randomUUID();
    const device = crypto.randomUUID();
    const episode = { ...input('M0 preserved'), id, created_at: time, updated_at: time };
    const active: LocalRecord = { record_type: 'functional_episode', record_id: id, local_revision: 8, local_change_id: crypto.randomUUID(), previous_local_change_id: null, device_id: device, updated_at: time, deleted: false, episode };
    const removed: LocalRecord = { ...active, record_id: tombstone, local_change_id: crypto.randomUUID(), deleted: true, episode: null };
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      request.onupgradeneeded = () => { request.result.createObjectStore('episodes', { keyPath: 'record_id' }); request.result.createObjectStore('installation'); };
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction(['episodes', 'installation'], 'readwrite');
        tx.objectStore('episodes').put(active); tx.objectStore('episodes').put(removed); tx.objectStore('installation').put(device, 'device_id');
        tx.oncomplete = () => { db.close(); resolve(); }; tx.onabort = () => reject(tx.error);
      };
      request.onerror = () => reject(request.error);
    });
    const a = await open(name);
    expect((await a.list())[0].episode).toEqual(episode);
    expect((await a.get(tombstone))?.deleted).toBe(true);
    const history = await a.history();
    expect(history).toHaveLength(1);
    expect(history[0]).toMatchObject({ operation: 'create', revision: 1, parent_ids: [], change_id: active.local_change_id, episode });
    a.close();
    const reopened = await open(name);
    expect(await reopened.history()).toEqual(history);
    expect((await reopened.get(tombstone))?.deleted).toBe(true);
  });

  it('creates immutable descendants, tombstones, and survives restart', async () => {
    const name = crypto.randomUUID(); const a = await open(name);
    const one = await a.create(input('First'));
    const two = await a.update({ ...one.episode!, notes: 'different' }, one.local_revision);
    await a.delete(two.record_id, two.local_revision);
    const history = await a.history();
    expect(history.map((c) => c.operation)).toEqual(['create', 'update', 'delete']);
    expect(history[0].revision).toBe(1);
    expect(history[1].parent_ids).toEqual([history[0].change_id]);
    expect(history[2].parent_ids).toEqual([history[1].change_id]);
    expect(history[2].episode).toBeNull();
    a.close(); const b = await open(name);
    expect(await b.history()).toEqual(history);
    expect(await b.list()).toEqual([]);
  });

  it('converges independent creates, edits, deletion and duplicate delivery', async () => {
    const a = await open(); const b = await open(); const box = mailbox();
    expect(await a.deviceId()).not.toBe(await b.deviceId());
    const first = await a.create(input('Phone')); const other = await b.create(input('Desktop'));
    await sync(a, box); await sync(b, box); await sync(a, box); await sync(a, box);
    expect((await a.list()).map((r) => r.record_id).sort()).toEqual([first.record_id, other.record_id].sort());
    expect(await a.history()).toHaveLength(2);
    const current = (await a.get(first.record_id))!;
    const updated = await a.update({ ...current.episode!, activity: 'Phone edited' }, current.local_revision);
    await sync(a, box); await sync(b, box);
    expect((await b.get(first.record_id))?.episode?.activity).toBe('Phone edited');
    await b.delete(first.record_id, updated.local_revision);
    await sync(b, box); await sync(a, box);
    expect((await a.get(first.record_id))?.deleted).toBe(true);
    const ancestor = (await a.history()).find((c) => c.change_id === first.local_change_id)!;
    await a.apply([canonicalJson(ancestor)], provider);
    expect((await a.get(first.record_id))?.deleted).toBe(true);
    expect((await a.get(other.record_id))?.deleted).toBe(false);
  });

  it.each(['edit', 'delete'] as const)('persists concurrent %s/edit conflicts and resolves with all tips', async (left) => {
    const name = crypto.randomUUID(); const a = await open(name); const b = await open(); const box = mailbox();
    const root = await a.create(input('Shared')); await sync(a, box); await sync(b, box);
    const other = await a.create(input('Unrelated'));
    if (left === 'delete') await a.delete(root.record_id, root.local_revision);
    else await a.update({ ...root.episode!, notes: 'left version' }, root.local_revision);
    const remote = (await b.get(root.record_id))!;
    const right = await b.update({ ...remote.episode!, notes: 'right version' }, remote.local_revision);
    await sync(a, box); await sync(b, box); await sync(a, box);
    const conflict = (await a.conflicts())[0];
    expect(conflict.candidates).toHaveLength(2);
    expect(conflict.conflict.base).toBe(root.local_change_id);
    await expect(a.update(root.episode!, 2)).rejects.toThrow();
    await expect(a.delete(root.record_id, 2)).rejects.toThrow();
    await a.update({ ...other.episode!, notes: 'unrelated works' }, other.local_revision);
    a.close(); const reopened = await open(name);
    expect(await reopened.conflicts()).toHaveLength(1);
    await reopened.resolve(root.record_id, right.local_change_id);
    const resolution = (await reopened.history()).find((change) => change.operation === 'resolve')!;
    expect(resolution.parent_ids).toEqual(conflict.candidates.map((change) => change.change_id).sort());
    expect((await reopened.get(root.record_id))?.episode?.notes).toBe('right version');
    await sync(reopened, box); await sync(b, box);
    expect(await b.conflicts()).toHaveLength(0);
    expect((await b.get(root.record_id))?.episode?.notes).toBe('right version');
  });

  it('persists acknowledgements after upload failure boundaries and retries merges', async () => {
    const a = await open(); const b = await open(); const box = mailbox();
    await a.create(input('Safe offline'));
    const failing = { ...box, pullChanges: vi.fn().mockRejectedValue(new Error('offline')) };
    await expect(syncDisposableEntries(a, provider, failing)).rejects.toThrow('offline');
    expect(await a.pending(provider)).toHaveLength(0);
    expect((await a.list())).toHaveLength(1);
    await sync(a, box);
    const apply = vi.spyOn(b, 'apply').mockRejectedValueOnce(new Error('transaction failed'));
    await expect(sync(b, box)).rejects.toThrow('transaction failed');
    expect(await b.history()).toHaveLength(0);
    expect(await b.pending(provider)).toHaveLength(0);
    apply.mockRestore(); await sync(b, box);
    expect((await b.list())).toHaveLength(1);
    expect(await b.pending(provider)).toHaveLength(0);
  });

  it('keeps new local work pending when it arrives during a sync', async () => {
    const store = await open();
    const box = mailbox();
    await store.create(input('First'));
    const delayed = {
      ...box,
      async pullChanges() {
        await store.create(input('Added during sync'));
        return box.pullChanges();
      },
    };
    await syncDisposableEntries(store, provider, delayed);
    const pending = await store.pending(provider);
    expect(pending).toHaveLength(1);
    expect(pending[0].episode?.activity).toBe('Added during sync');
  });

  it('rolls back a failed local transaction and rejects a stale same-generation head', async () => {
    const a = await open(); const original = await a.create(input('Unchanged'));
    const add = vi.spyOn(IDBObjectStore.prototype, 'add').mockImplementationOnce(() => { throw new Error('storage unavailable'); });
    await expect(a.update({ ...original.episode!, notes: 'unsaved' }, original.local_revision)).rejects.toThrow('storage unavailable');
    add.mockRestore();
    expect(await a.history()).toHaveLength(1);
    expect(await a.get(original.record_id)).toEqual(original);
    await expect(a.update(original.episode!, original.local_revision, crypto.randomUUID())).rejects.toThrow('another tab');
    expect(await a.get(original.record_id)).toEqual(original);
  });

  it('validates Python fixture history and rejects missing or corrupted ancestry atomically', async () => {
    const a = await open();
    const [root, deleted] = fixture.changes;
    await expect(a.apply([canonicalJson(deleted)], provider)).rejects.toThrow('parent');
    expect(await a.history()).toHaveLength(0);
    await a.apply([canonicalJson(root), canonicalJson(deleted)], provider);
    expect(await a.list()).toHaveLength(0);
    expect(await a.history()).toHaveLength(2);
  });

  it('accepts the Python UUIDv5 legacy baseline and rejects a forged reserved origin', async () => {
    const a = await open();
    const legacy = { ...fixture.changes[0], change_id: '0a75b12b-76ea-5960-88bd-05925c2cf809', device_id: '00000000-0000-0000-0000-000000000000' };
    await expect(a.apply([canonicalJson({ ...legacy, change_id: crypto.randomUUID() })], provider)).rejects.toThrow('legacy baseline');
    await a.apply([canonicalJson(legacy)], provider);
    expect((await a.list())[0].episode).toEqual(legacy.episode);
  });

  it('matches the Python deterministic conflict identity for identical concurrent roots', async () => {
    const first = await parseCanonicalChange(canonicalJson({ ...fixture.changes[0], change_id: '00000000-0000-0000-0000-000000000001' }));
    const second = await parseCanonicalChange(canonicalJson({ ...fixture.changes[0], change_id: '00000000-0000-0000-0000-000000000002' }));
    const conflicts: SyncConflict[] = [];
    await reconcile(new Map([[first.change_id, first]]), new Map([[first.record_id, first.change_id]]), conflicts, [second]);
    expect(conflicts[0].id).toBe('2444d5bf-a42f-5298-8afb-0ab86011002b');
  });
});
