import {
  collection,
  doc,
  getDocFromServer,
  getDocsFromServer,
  orderBy,
  query,
  runTransaction,
  serverTimestamp,
  Timestamp,
  type Firestore,
} from 'firebase/firestore';
import { uuidPattern } from '../model';
import { parseCanonicalChange, Z5PacketError, type Z5Change } from './z5-wire';

const IDENTITY_KEYS = ['owner_uid', 'journal_id', 'contract_version'];
const DESCRIPTOR_KEYS = ['owner_uid', 'journal_id', 'journal_kind', 'contract_version', 'record_schema_version', 'published_at'];
const CHANGE_DOCUMENT_KEYS = ['owner_uid', 'journal_id', 'change_id', 'record_id', 'contract_version',
  'record_schema_version', 'content_hash', 'canonical_payload_json', 'uploaded_at'];

export interface ProductionJournalDescriptor {
  ownerUid: string;
  journalId: string;
  journalKind: 'production';
  contractVersion: 1;
  recordSchemaVersion: 1;
  publishedAt: Timestamp;
}

export class SandboxIdentityCollisionError extends Error {
  constructor() {
    super('This change ID already exists with a different canonical payload. The existing sandbox packet was not changed.');
    this.name = 'SandboxIdentityCollisionError';
  }
}

export interface PulledSandboxChange {
  change: Z5Change;
  canonicalPayload: string;
  uploadedAt: Timestamp;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function assertUid(uid: string): void {
  if (!uid || uid.includes('/')) throw new Error('Firebase returned an invalid sandbox account identity.');
}

export function sandboxJournalPath(uid: string, journalId: string): string {
  assertUid(uid);
  if (!uuidPattern.test(journalId) || journalId !== journalId.toLowerCase()) throw new Error('Invalid sandbox journal identity.');
  return `users/${uid}/journals/${journalId}`;
}

export function parseProductionJournalDescriptor(
  data: unknown,
  documentId: string,
  uid: string,
): ProductionJournalDescriptor {
  assertUid(uid);
  if (!uuidPattern.test(documentId) || documentId !== documentId.toLowerCase() || documentId === '00000000-0000-0000-0000-000000000000') {
    throw new Z5PacketError('A production journal descriptor has an invalid journal identity.');
  }
  if (!object(data) || !exactKeys(data, DESCRIPTOR_KEYS) || data.owner_uid !== uid || data.journal_id !== documentId
      || data.journal_kind !== 'production' || data.contract_version !== 1 || data.record_schema_version !== 1
      || !(data.published_at instanceof Timestamp)) {
    throw new Z5PacketError('A production journal descriptor is malformed or incompatible.');
  }
  return {
    ownerUid: uid,
    journalId: documentId,
    journalKind: 'production',
    contractVersion: 1,
    recordSchemaVersion: 1,
    publishedAt: data.published_at,
  };
}

async function readSandboxJournalIdIfPresent(firestore: Firestore, uid: string): Promise<string | null> {
  const snapshot = await getDocFromServer(doc(firestore, 'users', uid, 'sandbox', 'identity'));
  if (!snapshot.exists()) return null;
  const data: unknown = snapshot.data();
  if (object(data) && typeof data.journal_id === 'string') return data.journal_id;
  return null;
}

export async function discoverProductionJournals(
  firestore: Firestore,
  uid: string,
): Promise<ProductionJournalDescriptor[]> {
  assertUid(uid);
  const sandboxJournalId = await readSandboxJournalIdIfPresent(firestore, uid);
  const snapshot = await getDocsFromServer(collection(firestore, 'users', uid, 'journals'));
  const descriptors = snapshot.docs.map((document) =>
    parseProductionJournalDescriptor(document.data(), document.id, uid));
  if (sandboxJournalId && descriptors.some((descriptor) => descriptor.journalId === sandboxJournalId)) {
    throw new Z5PacketError('Sandbox journal identity cannot be used as a production journal.');
  }
  return descriptors.sort((a, b) => a.journalId.localeCompare(b.journalId));
}

export async function verifyProductionJournalDescriptor(
  firestore: Firestore,
  uid: string,
  journalId: string,
): Promise<ProductionJournalDescriptor> {
  assertUid(uid);
  if (!uuidPattern.test(journalId) || journalId !== journalId.toLowerCase() || journalId === '00000000-0000-0000-0000-000000000000') {
    throw new Z5PacketError('Invalid production journal identity.');
  }
  const sandboxJournalId = await readSandboxJournalIdIfPresent(firestore, uid);
  if (sandboxJournalId === journalId) {
    throw new Z5PacketError('Sandbox journal identity cannot be used as a production journal.');
  }
  const snapshot = await getDocFromServer(doc(firestore, 'users', uid, 'journals', journalId));
  if (!snapshot.exists()) {
    throw new Z5PacketError('No compatible production journal is available for this Google account.');
  }
  return parseProductionJournalDescriptor(snapshot.data(), snapshot.id, uid);
}

export async function ensureProductionJournalDescriptor(
  firestore: Firestore,
  uid: string,
  journalId: string,
): Promise<'published' | 'already-published'> {
  assertUid(uid);
  if (!uuidPattern.test(journalId) || journalId !== journalId.toLowerCase() || journalId === '00000000-0000-0000-0000-000000000000') {
    throw new Z5PacketError('Invalid production journal identity.');
  }
  const descriptor = doc(firestore, 'users', uid, 'journals', journalId);
  return runTransaction(firestore, async (transaction) => {
    const existing = await transaction.get(descriptor);
    if (existing.exists()) {
      parseProductionJournalDescriptor(existing.data(), descriptor.id, uid);
      return 'already-published';
    }
    transaction.set(descriptor, {
      owner_uid: uid,
      journal_id: journalId,
      journal_kind: 'production',
      contract_version: 1,
      record_schema_version: 1,
      published_at: serverTimestamp(),
    });
    return 'published';
  });
}

export async function getOrCreateSandboxJournalId(firestore: Firestore, uid: string): Promise<string> {
  assertUid(uid);
  const identity = doc(firestore, 'users', uid, 'sandbox', 'identity');
  const candidate = crypto.randomUUID().toLowerCase();
  return runTransaction(firestore, async (transaction) => {
    const snapshot = await transaction.get(identity);
    if (snapshot.exists()) {
      const data: unknown = snapshot.data();
      if (!object(data) || !exactKeys(data, IDENTITY_KEYS) || data.owner_uid !== uid || data.contract_version !== 1
          || typeof data.journal_id !== 'string' || !uuidPattern.test(data.journal_id)
          || data.journal_id !== data.journal_id.toLowerCase()) {
        throw new Error('The sandbox journal identity is malformed; no cloud data was changed.');
      }
      return data.journal_id;
    }
    transaction.set(identity, { owner_uid: uid, journal_id: candidate, contract_version: 1 });
    return candidate;
  });
}

async function parseStoredChange(
  data: unknown,
  documentId: string,
  uid: string,
  journalId: string,
): Promise<PulledSandboxChange> {
  if (!object(data) || !exactKeys(data, CHANGE_DOCUMENT_KEYS) || data.owner_uid !== uid || data.journal_id !== journalId
      || data.change_id !== documentId || data.contract_version !== 1 || data.record_schema_version !== 1
      || typeof data.record_id !== 'string' || typeof data.content_hash !== 'string'
      || typeof data.canonical_payload_json !== 'string' || !(data.uploaded_at instanceof Timestamp)) {
    throw new Error('A sandbox transport document is malformed; no packet was applied.');
  }
  const change = await parseCanonicalChange(data.canonical_payload_json);
  if (change.change_id !== documentId || change.record_id !== data.record_id || change.content_hash !== data.content_hash) {
    throw new Error('A sandbox transport document does not match its canonical Z5 packet; no packet was applied.');
  }
  return { change, canonicalPayload: data.canonical_payload_json, uploadedAt: data.uploaded_at };
}

export class FirestoreSandboxTransport {
  constructor(private readonly firestore: Firestore, private readonly uid: string, readonly journalId: string) {
    assertUid(uid);
    sandboxJournalPath(uid, journalId);
  }

  private changeCollection() {
    return collection(this.firestore, 'users', this.uid, 'journals', this.journalId, 'changes');
  }

  async uploadCanonicalChange(canonicalPayload: string): Promise<'uploaded' | 'already-uploaded'> {
    const change = await parseCanonicalChange(canonicalPayload);
    const reference = doc(this.changeCollection(), change.change_id);
    return runTransaction(this.firestore, async (transaction) => {
      const existing = await transaction.get(reference);
      if (existing.exists()) {
        const stored = await parseStoredChange(existing.data(), reference.id, this.uid, this.journalId);
        if (stored.canonicalPayload === canonicalPayload) return 'already-uploaded';
        throw new SandboxIdentityCollisionError();
      }
      transaction.set(reference, {
        owner_uid: this.uid,
        journal_id: this.journalId,
        change_id: change.change_id,
        record_id: change.record_id,
        contract_version: change.contract_version,
        record_schema_version: change.record_schema_version,
        content_hash: change.content_hash,
        canonical_payload_json: canonicalPayload,
        uploaded_at: serverTimestamp(),
      });
      return 'uploaded';
    });
  }

  async pullChanges(): Promise<PulledSandboxChange[]> {
    const snapshot = await getDocsFromServer(query(this.changeCollection(), orderBy('change_id')));
    const changes = await Promise.all(snapshot.docs.map((document) =>
      parseStoredChange(document.data(), document.id, this.uid, this.journalId)));
    return changes;
  }
}
