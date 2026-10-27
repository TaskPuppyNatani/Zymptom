import {
  collection,
  doc,
  getDocsFromServer,
  orderBy,
  query,
  runTransaction,
  serverTimestamp,
  Timestamp,
  type Firestore,
} from 'firebase/firestore';
import { uuidPattern } from '../model';
import { parseCanonicalChange, type Z5Change } from './z5-wire';

const IDENTITY_KEYS = ['owner_uid', 'journal_id', 'contract_version'];
const CHANGE_DOCUMENT_KEYS = ['owner_uid', 'journal_id', 'change_id', 'record_id', 'contract_version',
  'record_schema_version', 'content_hash', 'canonical_payload_json', 'uploaded_at'];

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
