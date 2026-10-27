import { validateEpisode, utc, uuidPattern, type Episode } from '../model';

export interface Z5Change {
  contract_version: 1;
  record_schema_version: 1;
  record_type: 'functional_episode';
  change_id: string;
  record_id: string;
  revision: number;
  parent_ids: string[];
  operation: 'create' | 'update' | 'delete' | 'resolve';
  updated_at: string;
  device_id: string;
  deleted: boolean;
  content_hash: string;
  episode: Episode | null;
}

export interface Z5Batch {
  contract_version: 1;
  record_schema_version: 1;
  changes: Z5Change[];
}

export class Z5PacketError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'Z5PacketError';
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (!isObject(value)) return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortKeys(value[key])]));
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortKeys(value));
}

function canonicalUuid(value: unknown, label: string): asserts value is string {
  if (typeof value !== 'string' || !uuidPattern.test(value) || value !== value.toLowerCase()) {
    throw new Z5PacketError(`Invalid canonical ${label}.`);
  }
}

async function sha256(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export async function contentHash(recordId: string, episode: Episode | null): Promise<string> {
  if (episode === null) return sha256({ id: recordId, deleted: true, record_type: 'functional_episode' });
  const { updated_at: _auditTimestamp, ...meaningfulContent } = episode;
  return sha256(meaningfulContent);
}

const CHANGE_KEYS = ['change_id', 'record_id', 'revision', 'parent_ids', 'operation', 'updated_at', 'device_id',
  'deleted', 'content_hash', 'episode', 'contract_version', 'record_schema_version', 'record_type'];
const BATCH_KEYS = ['contract_version', 'record_schema_version', 'changes'];

async function validateChange(value: unknown): Promise<Z5Change> {
  if (!isObject(value) || !hasExactKeys(value, CHANGE_KEYS)) throw new Z5PacketError('Change fields do not match Z5 contract v1.');
  if (value.contract_version !== 1 || value.record_schema_version !== 1) throw new Z5PacketError('Unsupported Z5 contract version.');
  if (value.record_type !== 'functional_episode') throw new Z5PacketError('Unsupported Z5 record type.');
  canonicalUuid(value.change_id, 'change ID');
  canonicalUuid(value.record_id, 'record ID');
  canonicalUuid(value.device_id, 'device ID');
  if (!Number.isSafeInteger(value.revision) || (value.revision as number) < 1) throw new Z5PacketError('Invalid Z5 revision.');
  const parents = value.parent_ids;
  if (!Array.isArray(parents)) throw new Z5PacketError('Z5 parent IDs must be an array.');
  for (const parent of parents) canonicalUuid(parent, 'parent ID');
  const sortedParents = [...parents].sort();
  if (sortedParents.some((parent, index) => parent !== parents[index]) || new Set(sortedParents).size !== sortedParents.length) {
    throw new Z5PacketError('Z5 parent IDs must be unique and canonically ordered.');
  }
  if (!['create', 'update', 'delete', 'resolve'].includes(String(value.operation))) throw new Z5PacketError('Unsupported Z5 operation.');
  if (typeof value.updated_at !== 'string' || utc(value.updated_at) !== value.updated_at) throw new Z5PacketError('Z5 timestamps must be canonical UTC strings.');
  if (typeof value.deleted !== 'boolean') throw new Z5PacketError('Invalid Z5 tombstone state.');
  if (value.episode !== null) {
    try { validateEpisode(value.episode); }
    catch { throw new Z5PacketError('The Z5 episode snapshot is invalid.'); }
    if (value.episode.id !== value.record_id || value.episode.updated_at !== value.updated_at) {
      throw new Z5PacketError('The Z5 snapshot identity does not match its change.');
    }
    for (const field of ['occurred_at', 'created_at', 'updated_at'] as const) {
      if (utc(value.episode[field]) !== value.episode[field]) throw new Z5PacketError('Z5 snapshot timestamps must be canonical UTC strings.');
    }
  }
  if (value.deleted !== (value.episode === null)) throw new Z5PacketError('The Z5 tombstone and snapshot disagree.');
  if ((value.operation === 'create' || value.operation === 'update') && value.episode === null) {
    throw new Z5PacketError('The Z5 operation and snapshot disagree.');
  }
  if (value.operation === 'delete' && value.episode !== null) throw new Z5PacketError('The Z5 operation and snapshot disagree.');
  if (typeof value.content_hash !== 'string' || !/^[0-9a-f]{64}$/.test(value.content_hash)) {
    throw new Z5PacketError('Invalid Z5 content hash.');
  }
  if (await contentHash(value.record_id, value.episode as Episode | null) !== value.content_hash) {
    throw new Z5PacketError('The Z5 content hash does not match its snapshot.');
  }
  return value as unknown as Z5Change;
}

function readJson(raw: string): unknown {
  if (typeof raw !== 'string' || raw.length === 0 || new TextEncoder().encode(raw).byteLength > 900_000) {
    throw new Z5PacketError('The canonical Z5 packet is empty or exceeds the sandbox size limit.');
  }
  try { return JSON.parse(raw) as unknown; }
  catch { throw new Z5PacketError('Malformed canonical Z5 JSON.'); }
}

export async function parseCanonicalChange(raw: string): Promise<Z5Change> {
  const parsed = readJson(raw);
  const change = await validateChange(parsed);
  if (canonicalJson(change) !== raw) throw new Z5PacketError('The Z5 Change is not in canonical JSON form.');
  return change;
}

function compareChanges(left: Z5Change, right: Z5Change): number {
  if (left.revision !== right.revision) return left.revision - right.revision;
  if (left.record_id !== right.record_id) return left.record_id < right.record_id ? -1 : 1;
  return left.change_id === right.change_id ? 0 : left.change_id < right.change_id ? -1 : 1;
}

export async function parseCanonicalBatch(raw: string): Promise<Z5Batch> {
  const parsed = readJson(raw);
  if (!isObject(parsed) || !hasExactKeys(parsed, BATCH_KEYS)) throw new Z5PacketError('Batch fields do not match Z5 contract v1.');
  if (parsed.contract_version !== 1 || parsed.record_schema_version !== 1) throw new Z5PacketError('Unsupported Z5 contract version.');
  if (!Array.isArray(parsed.changes)) throw new Z5PacketError('Z5 batch changes must be an array.');
  const changes = await Promise.all(parsed.changes.map(validateChange));
  const ids = new Set(changes.map((change) => change.change_id));
  if (ids.size !== changes.length) throw new Z5PacketError('A Z5 batch repeats a change identity.');
  const batch: Z5Batch = { contract_version: 1, record_schema_version: 1, changes: [...changes].sort(compareChanges) };
  if (canonicalJson(batch) !== raw) throw new Z5PacketError('The Z5 batch is not in canonical JSON form.');
  return batch;
}

export async function makeSyntheticCollisionPayload(change: Z5Change): Promise<string> {
  if (!change.episode) throw new Z5PacketError('A tombstone cannot be used for the sandbox collision demonstration.');
  const episode = { ...change.episode, notes: `${change.episode.notes ?? ''}\nSandbox identity collision test` };
  const candidate: Z5Change = { ...change, episode, content_hash: await contentHash(change.record_id, episode) };
  return canonicalJson(candidate);
}
