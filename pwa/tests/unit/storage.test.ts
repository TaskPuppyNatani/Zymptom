import 'fake-indexeddb/auto';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fixture from '../fixtures/z5-v1.json';
import { blankEpisode, tagFields, type Episode, type EpisodeInput } from '../../src/model';
import { EpisodeStore } from '../../src/storage';

let store: EpisodeStore;
let name: string;
beforeEach(async () => { name = crypto.randomUUID(); store = await EpisodeStore.open(name); });
afterEach(() => store.close());

function input(): EpisodeInput { return { ...blankEpisode(), activity: 'DEMO: Café ☕\n雪', outcome: 'stopped_early' }; }

describe('browser-local episode store', () => {
  it('persists create/edit/delete, UUID and device identity through reopening without network', async () => {
    const identity = await store.deviceId();
    const created = await store.create({ ...input(), duration_minutes: 0, recovery_required: false, recovery_minutes: null });
    expect(created.local_revision).toBe(1);
    store.close();
    store = await EpisodeStore.open(name);
    expect(await store.deviceId()).toBe(identity);
    expect(await store.list()).toEqual([created]);
    const edited = await store.update({ ...created.episode!, notes: 'Line one\nLine two 雪' }, created.local_revision);
    expect(edited.record_id).toBe(created.record_id);
    expect(edited.local_revision).toBe(2);
    expect(edited.previous_local_change_id).toBe(created.local_change_id);
    expect(edited.episode!.created_at).toBe(created.episode!.created_at);
    expect(edited.episode!.duration_minutes).toBe(0);
    expect(edited.episode!.recovery_required).toBe(false);
    expect(edited.episode!.recovery_minutes).toBeNull();
    const deleted = await store.delete(edited.record_id, edited.local_revision);
    expect(deleted.deleted).toBe(true);
    expect(deleted.episode).toBeNull();
    expect(deleted.local_revision).toBe(3);
    store.close();
    store = await EpisodeStore.open(name);
    expect(await store.list()).toEqual([]);
    expect(await store.get(created.record_id)).toEqual(deleted);
    await expect(store.update(edited.episode!, edited.local_revision)).rejects.toThrow('changed in another tab');
  });

  it.each(tagFields)('preserves order and normalizes duplicates only when saving %s', async (field) => {
    const draft = input();
    draft[field] = ['walking', 'memory', 'walking', 'sitting'];
    const created = await store.create(draft);
    expect(created.episode![field]).toEqual(['walking', 'memory', 'sitting']);
    expect(draft[field]).toEqual(['walking', 'memory', 'walking', 'sitting']);
    const edited = await store.update({ ...created.episode!, [field]: ['memory', 'memory', 'walking'] }, created.local_revision);
    expect(edited.episode![field]).toEqual(['memory', 'walking']);
  });

  it('round-trips desktop snapshots faithfully, preserving microseconds, strings, and ordered fields', async () => {
    const original = fixture.changes[0].episode as Episode;
    // Fixture loading is a test seam, not a journal-import feature.
    const { id: _, created_at: __, updated_at: ___, ...draft } = original;
    const record = await store.create(draft);
    expect(record.episode!.occurred_at).toBe(original.occurred_at);
    expect(record.episode!.functional_demands).toEqual(original.functional_demands);
    expect(record.episode!.help_notes).toBe('');
    expect(record.episode!.symptom_notes).toBe(original.symptom_notes);
  });

  it('rejects invalid data and stale writes atomically without affecting other episodes', async () => {
    const first = await store.create(input());
    const second = await store.create({ ...input(), activity: 'Other entry' });
    await expect(store.create({ ...input(), activity: ' ' })).rejects.toThrow();
    const latest = await store.update({ ...first.episode!, activity: 'New saved state' }, first.local_revision);
    await expect(store.update({ ...first.episode!, activity: 'Stale edit' }, first.local_revision)).rejects.toThrow('another tab');
    await expect(store.delete(first.record_id, first.local_revision)).rejects.toThrow('another tab');
    expect(await store.get(first.record_id)).toEqual(latest);
    expect(await store.get(second.record_id)).toEqual(second);
  });

  it('keeps installations/databases isolated and resets only disposable entries', async () => {
    const other = await EpisodeStore.open(crypto.randomUUID());
    try {
      const identity = await store.deviceId();
      await store.create(input());
      expect(await other.list()).toEqual([]);
      expect(await other.deviceId()).not.toBe(identity);
      await store.resetDisposableData();
      expect(await store.list()).toEqual([]);
      expect(await store.deviceId()).toBe(identity);
    } finally { other.close(); }
  });
});
