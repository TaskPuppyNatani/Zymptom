import type { EpisodeStore } from '../storage';
import { canonicalJson } from './z5-wire';

export interface DisposableMailbox {
  uploadCanonicalChange(payload: string): Promise<unknown>;
  pullChanges(): Promise<Array<{ canonicalPayload: string }>>;
}

// Each successful upload is acknowledged independently. Incoming changes and
// their provider receipts commit together inside EpisodeStore.apply.
export async function syncDisposableEntries(store: EpisodeStore, provider: string, mailbox: DisposableMailbox): Promise<number> {
  const pending = await store.pending(provider);
  for (const change of pending) {
    await mailbox.uploadCanonicalChange(canonicalJson(change));
    await store.acknowledge(provider, change.change_id);
  }
  const remote = await mailbox.pullChanges();
  await store.apply(remote.map((packet) => packet.canonicalPayload), provider);
  return remote.length;
}
