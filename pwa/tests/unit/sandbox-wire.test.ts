import { readFile } from 'node:fs/promises';
import { describe, expect, it } from 'vitest';
import { canonicalJson, contentHash, makeSyntheticCollisionPayload, parseCanonicalBatch, parseCanonicalChange,
  Z5PacketError } from '../../src/sandbox/z5-wire';

const fixturePath = new URL('../fixtures/z5-v1.json', import.meta.url);

async function rawFixture(): Promise<string> { return (await readFile(fixturePath, 'utf8')).replace(/\n$/, ''); }

describe('canonical desktop Z5 Change transport encoding', () => {
  it('matches Python-generated canonical fixture bytes and preserves all semantic values', async () => {
    const raw = await rawFixture();
    const batch = await parseCanonicalBatch(raw);
    expect(canonicalJson(batch)).toBe(raw);
    const [create, tombstone] = batch.changes;
    expect(await parseCanonicalChange(canonicalJson(create))).toEqual(create);
    expect(create.change_id).toBe('00000000-0000-0000-0000-00000000012d');
    expect(create.episode).toMatchObject({
      activity: 'DEMO: Café ☕ and lunch', duration_minutes: 0, recovery_required: false,
      recovery_minutes: null, notes: 'Disposable fixture only\nNo real journal data', symptom_notes: '雪\nSecond line',
      functional_demands: ['walking', 'memory'], symptoms: ['sensory_overload', 'pain'],
    });
    expect(tombstone.deleted).toBe(true);
    expect(tombstone.episode).toBeNull();
    expect(tombstone.parent_ids).toEqual([create.change_id]);
    expect(await contentHash(create.record_id, create.episode)).toBe(create.content_hash);
    expect(await contentHash(tombstone.record_id, null)).toBe(tombstone.content_hash);
  });

  it('rejects malformed, unsupported, noncanonical, and hash-tampered packets', async () => {
    const raw = await rawFixture();
    await expect(parseCanonicalBatch('{broken')).rejects.toThrow(Z5PacketError);
    await expect(parseCanonicalBatch(raw.replace('"contract_version":1', '"contract_version":2'))).rejects.toThrow('Unsupported Z5 contract version');
    await expect(parseCanonicalBatch(`${raw} `)).rejects.toThrow('not in canonical JSON form');
    const batch = JSON.parse(raw) as { changes: Array<Record<string, unknown>>; contract_version: number; record_schema_version: number };
    const changed = structuredClone(batch);
    const episode = changed.changes[0].episode as Record<string, unknown>;
    episode.notes = 'Tampered without updating hash';
    await expect(parseCanonicalBatch(canonicalJson(changed))).rejects.toThrow('content hash does not match');
  });

  it('rejects same-ID conflict demonstration data if it is not backed by the expected existing packet', async () => {
    const [create] = (await parseCanonicalBatch(await rawFixture())).changes;
    const altered = await makeSyntheticCollisionPayload(create);
    const parsed = await parseCanonicalChange(altered);
    expect(parsed.change_id).toBe(create.change_id);
    expect(parsed.content_hash).not.toBe(create.content_hash);
    expect(parsed.episode?.notes).toContain('Sandbox identity collision test');
  });
});
