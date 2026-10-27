import { nowUtc, type Episode } from './model';
import { canonicalJson, contentHash, parseCanonicalChange, type Z5Change } from './sandbox/z5-wire';

export interface SyncConflict {
  id: string;
  recordId: string;
  local: string;
  incoming: string;
  base: string | null;
  resolution: string | null;
}

async function uuid5(namespace: string, name: string): Promise<string> {
  const prefix = Uint8Array.from(namespace.match(/../g)!, (byte) => parseInt(byte, 16));
  const content = new TextEncoder().encode(name);
  const input = new Uint8Array(prefix.length + content.length);
  input.set(prefix); input.set(content, prefix.length);
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-1', input)).slice(0, 16);
  bytes[6] = (bytes[6] & 15) | 0x50;
  bytes[8] = (bytes[8] & 63) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export async function validateLegacyOrigin(change: Z5Change): Promise<void> {
  if (change.device_id !== '00000000-0000-0000-0000-000000000000') return;
  if (change.operation !== 'create' || !change.episode) throw new Error('Invalid reserved legacy origin.');
  const id = await uuid5('17895883494d515ba733cc7f59c27e5c', canonicalJson(change.episode));
  if (change.change_id !== id) throw new Error('Invalid reserved legacy baseline identity.');
}

export async function buildChange(recordId: string, episode: Episode | null, deviceId: string,
  operation: Z5Change['operation'], parents: Z5Change[], changeId: string = crypto.randomUUID(), timestamp = episode?.updated_at ?? nowUtc()): Promise<Z5Change> {
  const change: Z5Change = {
    contract_version: 1, record_schema_version: 1, record_type: 'functional_episode',
    change_id: changeId, record_id: recordId, revision: Math.max(0, ...parents.map((parent) => parent.revision)) + 1,
    parent_ids: parents.map((parent) => parent.change_id).sort(), operation, updated_at: timestamp,
    device_id: deviceId, deleted: episode === null, content_hash: await contentHash(recordId, episode), episode,
  };
  return parseCanonicalChange(canonicalJson(change));
}

export function ancestors(id: string, changes: Map<string, Z5Change>): Set<string> {
  const seen = new Set<string>();
  const pending = [id];
  while (pending.length) {
    const next = pending.pop()!;
    if (seen.has(next)) continue;
    const change = changes.get(next);
    if (!change) throw new Error('A parent revision is missing; sync its history first.');
    seen.add(next);
    pending.push(...change.parent_ids);
  }
  return seen;
}

export function frontier(recordId: string, changes: Map<string, Z5Change>): Z5Change[] {
  const values = [...changes.values()].filter((change) => change.record_id === recordId);
  const parents = new Set(values.flatMap((change) => change.parent_ids));
  return values.filter((change) => !parents.has(change.change_id)).sort((a, b) => a.change_id.localeCompare(b.change_id));
}

export function validateGraph(changes: Map<string, Z5Change>): void {
  for (const change of changes.values()) {
    const parents = change.parent_ids.map((id) => {
      const parent = changes.get(id);
      if (!parent) throw new Error('A parent revision is missing; sync its history first.');
      if (parent.record_id !== change.record_id || parent.revision >= change.revision) throw new Error('Invalid revision ancestry.');
      return parent;
    });
    if (change.revision !== Math.max(0, ...parents.map((parent) => parent.revision)) + 1) throw new Error('Invalid revision generation.');
    if (change.operation === 'create') {
      if (parents.length || change.deleted) throw new Error('Invalid create ancestry.');
    } else if (change.operation === 'update' || change.operation === 'delete') {
      if (parents.length !== 1 || parents[0].deleted) throw new Error('Invalid edit or deletion ancestry.');
      if (change.episode && change.episode.created_at !== parents[0].episode!.created_at) throw new Error('Creation timestamp changed.');
      if (change.operation === 'update' && change.content_hash === parents[0].content_hash) throw new Error('An update must change episode content.');
    } else {
      if (parents.length < 2 || parents.some((left) => parents.some((right) => left !== right && ancestors(right.change_id, changes).has(left.change_id)))) throw new Error('Invalid resolution ancestry.');
      if (!parents.some((parent) => parent.deleted === change.deleted && parent.content_hash === change.content_hash)) throw new Error('Resolution must choose a parent version.');
    }
  }
}

export async function reconcile(changes: Map<string, Z5Change>, heads: Map<string, string>, conflicts: SyncConflict[], incoming: Z5Change[]): Promise<void> {
  const newIds = new Set<string>();
  for (const change of incoming) {
    const existing = changes.get(change.change_id);
    if (existing && canonicalJson(existing) !== canonicalJson(change)) throw new Error('An immutable change ID was reused with different contents.');
    if (!existing) { changes.set(change.change_id, change); newIds.add(change.change_id); }
  }
  validateGraph(changes);
  const incomingMap = new Map(incoming.map((change) => [change.change_id, change]));
  for (const recordId of [...new Set(incoming.filter((change) => newIds.has(change.change_id)).map((change) => change.record_id))].sort()) {
    for (const candidate of frontier(recordId, incomingMap)) {
      if (!newIds.has(candidate.change_id)) continue;
      const headId = heads.get(recordId);
      if (!headId || ancestors(candidate.change_id, changes).has(headId)) {
        heads.set(recordId, candidate.change_id);
      } else if (!ancestors(headId, changes).has(candidate.change_id)) {
        const pair = [headId, candidate.change_id].sort();
        const id = await uuid5('99454a4c5f39588bb0ced00f477690d4', canonicalJson(pair));
        if (!conflicts.some((conflict) => conflict.id === id)) {
          const common = [...ancestors(headId, changes)].filter((id) => ancestors(candidate.change_id, changes).has(id));
          common.sort((a, b) => changes.get(b)!.revision - changes.get(a)!.revision || b.localeCompare(a));
          conflicts.push({ id, recordId, local: headId, incoming: candidate.change_id, base: common[0] ?? null, resolution: null });
        }
      }
    }
  }
  for (const change of incoming.filter((item) => newIds.has(item.change_id) && item.operation === 'resolve')) {
    const covered = ancestors(change.change_id, changes);
    for (const conflict of conflicts) if (!conflict.resolution && conflict.recordId === change.record_id && covered.has(conflict.local) && covered.has(conflict.incoming)) conflict.resolution = change.change_id;
  }
}
