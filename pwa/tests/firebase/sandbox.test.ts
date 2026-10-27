import { readFile } from 'node:fs/promises';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import {
  assertFails,
  assertSucceeds,
  initializeTestEnvironment,
  type RulesTestEnvironment,
} from '@firebase/rules-unit-testing';
import {
  collection,
  deleteDoc,
  doc,
  getDoc,
  getDocs,
  serverTimestamp,
  setDoc,
  updateDoc,
} from 'firebase/firestore';
import { canonicalJson, makeSyntheticCollisionPayload, parseCanonicalBatch, type Z5Change } from '../../src/sandbox/z5-wire';
import { getOrCreateSandboxJournalId, FirestoreSandboxTransport } from '../../src/sandbox/transport';
import 'fake-indexeddb/auto';
import { EpisodeStore } from '../../src/storage';
import { blankEpisode } from '../../src/model';
import { syncDisposableEntries } from '../../src/sandbox/disposable-sync';

const projectId = 'demo-zymptom-z51';
const rules = await readFile(new URL('../../firestore.rules', import.meta.url), 'utf8');
const fixtureText = (await readFile(new URL('../fixtures/z5-v1.json', import.meta.url), 'utf8')).replace(/\n$/, '');
const fixture = await parseCanonicalBatch(fixtureText);
const [changeA, changeB] = fixture.changes;
const journalA = '00000000-0000-0000-0000-0000000000a1';
const journalB = '00000000-0000-0000-0000-0000000000b2';
let environment: RulesTestEnvironment;

function identityDoc(owner: string, journal = journalA) {
  return { owner_uid: owner, journal_id: journal, contract_version: 1 };
}

function changeDoc(owner: string, journal: string, change: Z5Change = changeA) {
  return {
    owner_uid: owner,
    journal_id: journal,
    change_id: change.change_id,
    record_id: change.record_id,
    contract_version: change.contract_version,
    record_schema_version: change.record_schema_version,
    content_hash: change.content_hash,
    canonical_payload_json: canonicalJson(change),
    uploaded_at: serverTimestamp(),
  };
}

beforeAll(async () => {
  environment = await initializeTestEnvironment({ projectId, firestore: { rules } });
});

afterEach(async () => { await environment.clearFirestore(); });
afterAll(async () => { await environment.cleanup(); });

describe('Firestore Sandbox Sync security rules', () => {
  it('denies unauthenticated identity and change reads and writes', async () => {
    const db = environment.unauthenticatedContext().firestore();
    await assertFails(getDoc(doc(db, 'users', 'user-a', 'sandbox', 'identity')));
    await assertFails(getDocs(collection(db, 'users', 'user-a', 'journals', journalA, 'changes')));
    await assertFails(setDoc(doc(db, 'users', 'user-a', 'sandbox', 'identity'), identityDoc('user-a')));
    await assertFails(setDoc(doc(db, 'users', 'user-a', 'journals', journalA, 'changes', changeA.change_id), changeDoc('user-a', journalA)));
  });

  it('allows the matching authenticated owner to create/read its opaque identity and valid packet', async () => {
    const db = environment.authenticatedContext('user-a').firestore();
    const identity = doc(db, 'users', 'user-a', 'sandbox', 'identity');
    const packet = doc(db, 'users', 'user-a', 'journals', journalA, 'changes', changeA.change_id);
    await assertSucceeds(setDoc(identity, identityDoc('user-a')));
    await assertSucceeds(getDoc(identity));
    await assertSucceeds(setDoc(packet, changeDoc('user-a', journalA)));
    const stored = await assertSucceeds(getDoc(packet));
    expect(stored.data()?.canonical_payload_json).toBe(canonicalJson(changeA));
    await assertSucceeds(getDocs(collection(db, 'users', 'user-a', 'journals', journalA, 'changes')));
  });

  it('denies cross-UID identity reads and journal reads/writes', async () => {
    const owner = environment.authenticatedContext('user-a').firestore();
    const outsider = environment.authenticatedContext('user-b').firestore();
    await assertSucceeds(setDoc(doc(owner, 'users', 'user-a', 'sandbox', 'identity'), identityDoc('user-a')));
    await assertSucceeds(setDoc(doc(owner, 'users', 'user-a', 'journals', journalA, 'changes', changeA.change_id), changeDoc('user-a', journalA)));
    await assertFails(getDoc(doc(outsider, 'users', 'user-a', 'sandbox', 'identity')));
    await assertFails(getDoc(doc(outsider, 'users', 'user-a', 'journals', journalA, 'changes', changeA.change_id)));
    await assertFails(getDocs(collection(outsider, 'users', 'user-a', 'journals', journalA, 'changes')));
    await assertFails(setDoc(doc(outsider, 'users', 'user-a', 'journals', journalA, 'changes', changeB.change_id), changeDoc('user-b', journalA, changeB)));
    await assertFails(setDoc(doc(outsider, 'users', 'user-a', 'sandbox', 'identity'), identityDoc('user-b', journalB)));
  });

  it('denies mutation, deletion, and duplicate document overwrite after create', async () => {
    const db = environment.authenticatedContext('user-a').firestore();
    const packet = doc(db, 'users', 'user-a', 'journals', journalA, 'changes', changeA.change_id);
    const original = changeDoc('user-a', journalA);
    await assertSucceeds(setDoc(doc(db, 'users', 'user-a', 'sandbox', 'identity'), identityDoc('user-a')));
    await assertSucceeds(setDoc(packet, original));
    await assertFails(updateDoc(packet, { canonical_payload_json: '{}' }));
    await assertFails(deleteDoc(packet));
    await assertFails(setDoc(packet, original));
    expect((await getDoc(packet)).data()?.canonical_payload_json).toBe(canonicalJson(changeA));
  });

  it('rejects malformed identity and transport envelopes while accepting canonical fixture fields', async () => {
    const db = environment.authenticatedContext('user-a').firestore();
    const identity = doc(db, 'users', 'user-a', 'sandbox', 'identity');
    await assertFails(setDoc(identity, { ...identityDoc('user-a'), email: 'not-needed@example.test' }));
    await assertSucceeds(setDoc(identity, identityDoc('user-a')));
    const packet = doc(db, 'users', 'user-a', 'journals', journalA, 'changes', changeA.change_id);
    await assertFails(setDoc(packet, { ...changeDoc('user-a', journalA), content_hash: 'wrong' }));
    await assertFails(setDoc(packet, { ...changeDoc('user-a', journalA), change_id: changeB.change_id }));
    await assertFails(setDoc(packet, { ...changeDoc('user-a', journalA), journal_id: journalB }));
    await assertFails(setDoc(packet, { ...changeDoc('user-a', journalA), canonical_payload_json: '' }));
    await assertFails(setDoc(packet, { ...changeDoc('user-a', journalA), extra: 'field' }));
    await assertSucceeds(setDoc(packet, changeDoc('user-a', journalA)));
  });
});

describe('Firestore immutable transport adapter', () => {
  it('syncs real disposable IndexedDB episodes through the emulator mailbox', async () => {
    const db = environment.authenticatedContext('disposable-sync-user').firestore();
    const journal = await getOrCreateSandboxJournalId(db, 'disposable-sync-user');
    const transport = new FirestoreSandboxTransport(db, 'disposable-sync-user', journal);
    const a = await EpisodeStore.open(crypto.randomUUID());
    const b = await EpisodeStore.open(crypto.randomUUID());
    try {
      const first = await a.create({ ...blankEpisode(), activity: 'DEMO: emulator phone', outcome: 'stopped_early' });
      await b.create({ ...blankEpisode(), activity: 'DEMO: emulator desktop', outcome: 'completed_with_difficulty' });
      await syncDisposableEntries(a, journal, transport);
      await syncDisposableEntries(b, journal, transport);
      await syncDisposableEntries(a, journal, transport);
      expect(await a.list()).toHaveLength(2);
      expect(await b.list()).toHaveLength(2);
      await b.delete(first.record_id, first.local_revision);
      await syncDisposableEntries(b, journal, transport);
      await syncDisposableEntries(a, journal, transport);
      expect((await a.get(first.record_id))?.deleted).toBe(true);
      expect(await a.list()).toHaveLength(1);
    } finally { a.close(); b.close(); }
  });
  it('resolves one same-user journal across racing installations and exchanges exact fixture packets', async () => {
    const deviceA = environment.authenticatedContext('same-user').firestore();
    const deviceB = environment.authenticatedContext('same-user').firestore();
    const [journalForA, journalForB] = await Promise.all([
      getOrCreateSandboxJournalId(deviceA, 'same-user'),
      getOrCreateSandboxJournalId(deviceB, 'same-user'),
    ]);
    expect(journalForA).toBe(journalForB);
    const transportA = new FirestoreSandboxTransport(deviceA, 'same-user', journalForA);
    const transportB = new FirestoreSandboxTransport(deviceB, 'same-user', journalForB);
    const payloadA = canonicalJson(changeA);
    const payloadB = canonicalJson(changeB);
    expect(await transportA.uploadCanonicalChange(payloadA)).toBe('uploaded');
    const receivedByB = await transportB.pullChanges();
    expect(receivedByB.map((packet) => packet.canonicalPayload)).toEqual([payloadA]);
    expect(receivedByB[0].change).toEqual(changeA);
    expect(await transportB.uploadCanonicalChange(payloadB)).toBe('uploaded');
    expect(await transportB.uploadCanonicalChange(payloadA)).toBe('already-uploaded');
    const receivedByA = await transportA.pullChanges();
    expect(receivedByA.map((packet) => packet.canonicalPayload)).toEqual([payloadA, payloadB].sort((a, b) =>
      JSON.parse(a).change_id.localeCompare(JSON.parse(b).change_id)));
    expect(receivedByA.map((packet) => packet.change.change_id)).toEqual([changeA.change_id, changeB.change_id].sort());
  });

  it('rejects same-ID/different-valid-payload collisions without changing the first packet', async () => {
    const db = environment.authenticatedContext('collision-user').firestore();
    const journal = await getOrCreateSandboxJournalId(db, 'collision-user');
    const transport = new FirestoreSandboxTransport(db, 'collision-user', journal);
    const original = canonicalJson(changeA);
    const altered = await makeSyntheticCollisionPayload(changeA);
    expect(await transport.uploadCanonicalChange(original)).toBe('uploaded');
    await expect(transport.uploadCanonicalChange(altered)).rejects.toMatchObject({ name: 'SandboxIdentityCollisionError' });
    expect((await transport.pullChanges()).map((packet) => packet.canonicalPayload)).toEqual([original]);
  });

  it('isolates journal identities and packets between different Firebase UIDs', async () => {
    const userA = environment.authenticatedContext('user-a').firestore();
    const userB = environment.authenticatedContext('user-b').firestore();
    const [journalForA, journalForB] = await Promise.all([
      getOrCreateSandboxJournalId(userA, 'user-a'),
      getOrCreateSandboxJournalId(userB, 'user-b'),
    ]);
    expect(journalForA).not.toBe(journalForB);
    const transportA = new FirestoreSandboxTransport(userA, 'user-a', journalForA);
    const transportB = new FirestoreSandboxTransport(userB, 'user-b', journalForB);
    await transportA.uploadCanonicalChange(canonicalJson(changeA));
    expect(await transportB.pullChanges()).toEqual([]);
    await transportB.uploadCanonicalChange(canonicalJson(changeA));
    expect((await transportB.pullChanges()).map((packet) => packet.canonicalPayload)).toEqual([canonicalJson(changeA)]);
    expect((await transportA.pullChanges()).map((packet) => packet.canonicalPayload)).toEqual([canonicalJson(changeA)]);
    await assertFails(getDoc(doc(userB, 'users', 'user-a', 'journals', journalForA, 'changes', changeA.change_id)));
  });

  it('rejects malformed remote canonical payloads before exposing them to sandbox state', async () => {
    const owner = 'tampered-user';
    const db = environment.authenticatedContext(owner).firestore();
    const journal = await getOrCreateSandboxJournalId(db, owner);
    const reference = doc(db, 'users', owner, 'journals', journal, 'changes', changeA.change_id);
    await environment.withSecurityRulesDisabled(async (context) => {
      await setDoc(doc(context.firestore(), 'users', owner, 'journals', journal, 'changes', changeA.change_id), {
        ...changeDoc(owner, journal), canonical_payload_json: '{malformed',
      });
    });
    await expect(new FirestoreSandboxTransport(db, owner, journal).pullChanges()).rejects.toThrow('Malformed canonical Z5 JSON');
    expect((await getDoc(reference)).exists()).toBe(true);
  });
});
