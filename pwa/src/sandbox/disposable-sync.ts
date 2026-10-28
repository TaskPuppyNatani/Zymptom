import type { EpisodeStore } from '../storage';
import type { SyncConflict } from '../sync';
import { syncFailure, syncLabel, syncMessage, type SyncFailure, type SyncViewState } from './presentation';
import type { ProductionJournalDescriptor } from './transport';
import { canonicalJson, type Z5Change } from './z5-wire';

export interface DisposableMailbox {
  uploadCanonicalChange(payload: string): Promise<unknown>;
  pullChanges(): Promise<Array<{ canonicalPayload: string }>>;
}

export type ProductionSyncState =
  | 'checking'
  | 'unconfigured'
  | 'signed-out'
  | 'loading-journal'
  | 'awaiting-join'
  | 'no-journal'
  | 'signed-in'
  | 'failed';

export interface ProductionSyncUser {
  uid: string;
  email?: string | null;
}

export interface ProductionSyncDeps {
  discoverJournals: (uid: string) => Promise<ProductionJournalDescriptor[]>;
  verifyDescriptor: (uid: string, journalId: string) => Promise<ProductionJournalDescriptor>;
  createMailbox: (uid: string, journalId: string) => DisposableMailbox;
  signOut?: () => Promise<void>;
  onChange?: () => void;
}

export function productionProvider(journalId: string): string {
  return `firebase-production:${journalId}`;
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

export async function joinProductionJournal(
  store: EpisodeStore,
  journalId: string,
  mailbox: DisposableMailbox,
): Promise<number> {
  const provider = productionProvider(journalId);
  const remote = await mailbox.pullChanges();
  await store.joinProductionJournal(journalId, remote.map((packet) => packet.canonicalPayload), provider);
  return remote.length;
}

export async function syncProductionEntries(
  store: EpisodeStore,
  journalId: string,
  mailbox: DisposableMailbox,
): Promise<{ pulled: number; uploaded: number }> {
  const currentJournalId = await store.journalId();
  if (!currentJournalId) {
    throw new Error('Join a production journal before syncing.');
  }
  if (currentJournalId !== journalId) {
    throw new Error('The mailbox journal identity does not match this production journal.');
  }
  const provider = productionProvider(journalId);
  const remote = await mailbox.pullChanges();
  await store.apply(remote.map((packet) => packet.canonicalPayload), provider);
  const pending = await store.pending(provider);
  let uploaded = 0;
  for (const change of pending) {
    await mailbox.uploadCanonicalChange(canonicalJson(change));
    await store.acknowledge(provider, change.change_id);
    uploaded += 1;
  }
  return { pulled: remote.length, uploaded };
}

export class ProductionSyncController {
  state: ProductionSyncState = 'signed-out';
  user: ProductionSyncUser | null = null;
  joinedJournalId: string | null = null;
  discovered: ProductionJournalDescriptor[] = [];
  selectedJournalId: string | null = null;
  conflicts: Array<{ conflict: SyncConflict; candidates: Z5Change[] }> = [];
  busy = false;
  connecting = false;
  operation?: 'disconnect' | 'resolving';
  successful = false;
  failure?: SyncFailure;
  lastSync?: Date;
  notice?: string;
  private mailbox?: DisposableMailbox;
  private store?: EpisodeStore;
  private onActivated?: () => Promise<void>;
  private generation = 0;

  constructor(private readonly deps: ProductionSyncDeps) {}

  private notify(): void {
    this.deps.onChange?.();
  }

  async attachStore(store: EpisodeStore, onActivated?: () => Promise<void>): Promise<void> {
    this.store = store;
    this.onActivated = onActivated;
    this.joinedJournalId = await store.journalId();
    const savedSync = await store.lastSyncedAt();
    if (savedSync) this.lastSync = new Date(savedSync);
    if (this.joinedJournalId) await this.onActivated?.();
    await this.refreshConflicts();
  }

  async refreshConflicts(): Promise<void> {
    if (!this.store) return;
    this.conflicts = await this.store.conflicts();
    this.notify();
  }

  localChanged(): void {
    this.successful = false;
    this.notice = undefined;
    this.notify();
  }

  viewState(): SyncViewState {
    const connected = (this.state === 'signed-in' || this.state === 'awaiting-join' || this.state === 'no-journal') && !!this.user;
    return {
      connected,
      busy: this.busy,
      connecting: this.connecting || this.state === 'checking' || this.state === 'loading-journal',
      operation: this.operation,
      successful: this.state === 'signed-in' && this.successful,
      conflicts: this.conflicts.length,
      failure: this.failure,
    };
  }

  statusLabel(): string {
    return syncLabel(this.viewState());
  }

  errorMessage(): string {
    return syncMessage(this.viewState());
  }

  async inspectAccount(user: ProductionSyncUser): Promise<void> {
    const generation = ++this.generation;
    if (this.user?.uid !== user.uid) this.successful = false;
    this.user = user;
    this.mailbox = undefined;
    this.discovered = [];
    this.selectedJournalId = null;
    this.state = 'loading-journal';
    this.failure = undefined;
    this.notice = undefined;
    this.notify();
    try {
      if (this.joinedJournalId !== null) {
        await this.deps.verifyDescriptor(user.uid, this.joinedJournalId);
        if (generation !== this.generation) return;
        this.mailbox = this.deps.createMailbox(user.uid, this.joinedJournalId);
        this.state = 'signed-in';
        const pending = this.store ? await this.store.pending(productionProvider(this.joinedJournalId)) : [];
        this.successful = Boolean(this.lastSync) && pending.length === 0 && this.conflicts.length === 0;
      } else {
        const journals = await this.deps.discoverJournals(user.uid);
        if (generation !== this.generation) return;
        this.discovered = journals;
        if (journals.length === 0) {
          this.selectedJournalId = null;
          this.state = 'no-journal';
          this.notice = 'No existing production journal was found for this Google account. Prepare and sync your journal on desktop first.';
        } else if (journals.length === 1) {
          this.selectedJournalId = journals[0].journalId;
          this.state = 'awaiting-join';
          this.notice = 'Existing production journal found. Confirm below to download and join it on this device.';
        } else {
          this.selectedJournalId = null;
          this.state = 'awaiting-join';
          this.notice = 'Multiple production journals were found. Select the journal you want to join, then confirm.';
        }
      }
    } catch (failure) {
      if (generation !== this.generation) return;
      if (this.joinedJournalId !== null && failure instanceof Error && failure.message.includes('No compatible production journal')) {
        this.mailbox = undefined;
        this.state = 'no-journal';
        this.notice = 'No compatible production journal is available for this Google account. Your local journal was not changed.';
      } else {
        this.state = 'failed';
        this.failure = syncFailure(failure);
      }
    }
    this.notify();
  }

  selectJournal(journalId: string): void {
    if (!this.discovered.some((descriptor) => descriptor.journalId === journalId)) return;
    this.selectedJournalId = journalId;
    this.notify();
  }

  async confirmJoin(): Promise<void> {
    if (this.busy || this.state !== 'awaiting-join' || !this.selectedJournalId || !this.store || !this.user) return;
    const targetJournalId = this.selectedJournalId;
    this.busy = true;
    this.failure = undefined;
    this.notify();
    try {
      await this.deps.verifyDescriptor(this.user.uid, targetJournalId);
      const mailbox = this.deps.createMailbox(this.user.uid, targetJournalId);
      await joinProductionJournal(this.store, targetJournalId, mailbox);
      this.joinedJournalId = targetJournalId;
      this.mailbox = mailbox;
      this.discovered = [];
      this.state = 'signed-in';
      this.lastSync = new Date();
      await this.store.setLastSyncedAt(this.lastSync.toISOString());
      await this.onActivated?.();
      await this.refreshConflicts();
      const provider = productionProvider(targetJournalId);
      this.successful = (await this.store.pending(provider)).length === 0 && this.conflicts.length === 0;
      this.notice = undefined;
    } catch (failure) {
      this.successful = false;
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      await this.refreshConflicts();
    }
  }

  async syncEntries(): Promise<void> {
    if (this.busy || this.state !== 'signed-in' || !this.mailbox || !this.joinedJournalId || !this.store || !this.user) return;
    this.busy = true;
    this.failure = undefined;
    this.notify();
    try {
      await this.deps.verifyDescriptor(this.user.uid, this.joinedJournalId);
      this.notice = undefined;
      await syncProductionEntries(this.store, this.joinedJournalId, this.mailbox);
      await this.onActivated?.();
      await this.refreshConflicts();
      const provider = productionProvider(this.joinedJournalId);
      this.successful = (await this.store.pending(provider)).length === 0 && this.conflicts.length === 0;
      this.lastSync = new Date();
      await this.store.setLastSyncedAt(this.lastSync.toISOString());
    } catch (failure) {
      this.successful = false;
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      await this.refreshConflicts();
    }
  }

  async resolveConflict(recordId: string, changeId: string): Promise<void> {
    if (!this.store || this.busy) return;
    this.busy = true;
    this.operation = 'resolving';
    this.failure = undefined;
    this.notify();
    try {
      await this.store.resolve(recordId, changeId);
      this.successful = false;
      await this.onActivated?.();
      await this.refreshConflicts();
      this.notice = 'Version saved. Sync now to share it.';
    } catch (failure) {
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      this.operation = undefined;
      await this.refreshConflicts();
    }
  }

  async disconnect(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.operation = 'disconnect';
    this.notify();
    try {
      await this.deps.signOut?.();
      this.user = null;
      this.mailbox = undefined;
      this.discovered = [];
      this.selectedJournalId = null;
      this.state = 'signed-out';
      this.successful = false;
      this.failure = undefined;
      if (this.joinedJournalId) {
        this.notice = 'Disconnected. Your production journal and unsynced changes remain on this device.';
      }
    } catch (failure) {
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      this.operation = undefined;
      this.notify();
    }
  }
}
