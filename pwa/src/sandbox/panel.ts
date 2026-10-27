import { onAuthStateChanged, type User } from 'firebase/auth';
import fixtureJson from './synthetic-v1.json?raw';
import { completeGoogleRedirect, firebaseErrorMessage, formatSafeFirebaseAuthErrorShape, initializeFirebaseRuntime,
  signInWithGoogle, signOutSandbox, type FirebaseEnvironment, type FirebaseRuntime } from './firebase';
import { getOrCreateSandboxJournalId, FirestoreSandboxTransport, SandboxIdentityCollisionError,
  type PulledSandboxChange } from './transport';
import { canonicalJson, makeSyntheticCollisionPayload, parseCanonicalBatch, type Z5Change } from './z5-wire';
import type { EpisodeStore } from '../storage';
import { syncDisposableEntries } from './disposable-sync';
import { candidateSummary, conflictExplanation, syncFailure, syncLabel, syncMessage, type SyncFailure, type SyncViewState } from './presentation';

type PanelState = 'checking' | 'unconfigured' | 'signed-out' | 'loading-journal' | 'signed-in' | 'failed';

function element<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function actionButton(text: string, callback: () => void, className = ''): HTMLButtonElement {
  const button = element('button', text, className);
  button.type = 'button'; button.addEventListener('click', callback); return button;
}
function standaloneDisplay(): boolean {
  return window.matchMedia('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone === true;
}

export class SandboxSyncPanel {
  readonly element = element('details', undefined, 'sandbox-panel');
  private readonly summary = element('summary', 'Sync · Sandbox');
  private readonly intro = element('p', 'Sandbox Sync · Disposable Data. Only test entries are shared; the desktop journal is separate.', 'sandbox-copy');
  private readonly authState = element('p', '', 'sandbox-state sync-heading');
  private readonly account = element('p', '', 'sandbox-copy');
  private readonly status = element('p', '', 'sandbox-state');
  private readonly error = element('p', '', 'error-message');
  private readonly controls = element('div', undefined, 'sandbox-actions');
  private readonly conflictList = element('div', undefined, 'sandbox-conflicts');
  private readonly developer = element('details', undefined, 'sync-developer');
  private readonly developerControls = element('div', undefined, 'sandbox-actions');
  private readonly developerStatus = element('p', '', 'sandbox-state');
  private readonly developerError = element('p', '', 'error-message');
  private readonly packetList = element('ul', undefined, 'sandbox-packets');
  private runtime?: FirebaseRuntime;
  private transport?: FirestoreSandboxTransport;
  private user: User | null = null;
  private state: PanelState = 'checking';
  private packets: PulledSandboxChange[] = [];
  private changes: Z5Change[] = [];
  private busy = false;
  private connecting = false;
  private operation?: 'disconnect' | 'resolving';
  private successful = false;
  private failure?: SyncFailure;
  private lastSync?: Date;
  private notice?: string;
  private conflictCount = 0;
  private reviewing = false;
  private generation = 0;
  private conflictGeneration = 0;
  private unsubscribe?: () => void;
  private store?: EpisodeStore;
  private refreshToday?: () => Promise<void>;

  constructor() {
    this.summary.id = 'sandbox-sync-heading';
    this.authState.setAttribute('role', 'status');
    this.status.setAttribute('role', 'status');
    this.error.setAttribute('role', 'alert');
    this.developerError.setAttribute('role', 'alert');
    this.packetList.setAttribute('aria-label', 'Pulled sandbox packet IDs');
    this.developer.append(element('summary', 'Developer details'), this.developerStatus, this.developerError, this.developerControls, this.packetList);
    this.element.append(this.summary, this.intro, this.authState, this.account, this.status, this.error, this.controls, this.conflictList, this.developer);
    this.render();
  }

  attachStore(store: EpisodeStore, refreshToday: () => Promise<void>): void {
    this.store = store; this.refreshToday = refreshToday; void this.renderConflicts();
  }
  localChanged(): void { this.successful = false; this.notice = undefined; this.render(); }
  private viewState(): SyncViewState {
    return { connected: this.state === 'signed-in' && !!this.user, busy: this.busy,
      connecting: this.connecting || this.state === 'checking' || this.state === 'loading-journal', operation: this.operation, successful: this.successful,
      conflicts: this.conflictCount, failure: this.failure };
  }

  private async renderConflicts(): Promise<void> {
    if (!this.store) return;
    const request = ++this.conflictGeneration;
    const conflicts = await this.store.conflicts();
    if (request !== this.conflictGeneration) return;
    this.conflictCount = conflicts.length;
    this.conflictList.replaceChildren();
    const seen = new Set<string>();
    if (this.reviewing) for (const { conflict, candidates } of conflicts) {
      if (seen.has(conflict.recordId)) continue;
      seen.add(conflict.recordId);
      const section = element('section', undefined, 'sync-conflict');
      section.append(element('h3', 'Review conflict'));
      section.append(element('p', conflictExplanation(candidates)));
      candidates.forEach((candidate, index) => {
        const card = element('div', undefined, 'sync-version');
        card.append(element('h4', `Version ${index + 1}`), element('p', candidateSummary(candidate), 'sync-version-text'));
        const choose = actionButton(index === 0 ? 'Keep this version' : index === 1 ? 'Keep the other version' : `Keep version ${index + 1}`,
          () => { void this.resolveConflict(conflict.recordId, candidate.change_id); });
        choose.disabled = this.busy;
        card.append(choose); section.append(card);
      });
      this.conflictList.append(section);
    }
    if (!conflicts.length) this.reviewing = false;
    this.render();
  }

  private async resolveConflict(recordId: string, changeId: string): Promise<void> {
    if (!this.store || this.busy) return;
    this.busy = true; this.operation = 'resolving'; this.failure = undefined; this.render();
    try {
      await this.store.resolve(recordId, changeId);
      this.successful = false;
      await this.refreshToday?.();
      await this.renderConflicts();
      this.notice = 'Version saved. Sync now to share it.';
    } catch (failure) { this.failure = syncFailure(failure); }
    finally { this.busy = false; this.operation = undefined; await this.renderConflicts(); }
  }

  async start(environment: FirebaseEnvironment): Promise<void> {
    try {
      const runtime = await initializeFirebaseRuntime(environment);
      if (!runtime) { this.state = 'unconfigured'; this.render(); return; }
      this.runtime = runtime;
      this.unsubscribe = onAuthStateChanged(runtime.auth, (user) => { void this.onUserChanged(user); }, (failure) => {
        this.state = 'failed'; this.failure = syncFailure(failure); this.render();
      });
      await completeGoogleRedirect(runtime.auth);
    } catch (failure) { this.state = 'failed'; this.failure = syncFailure(failure); }
    this.render();
  }
  dispose(): void { this.unsubscribe?.(); }

  private async onUserChanged(user: User | null): Promise<void> {
    const generation = ++this.generation;
    if (this.user?.uid !== user?.uid) { this.lastSync = undefined; this.successful = false; }
    this.user = user; this.transport = undefined; this.packets = []; this.failure = undefined; this.notice = undefined;
    this.developerStatus.textContent = ''; this.developerError.textContent = '';
    if (!user || !this.runtime) { this.state = 'signed-out'; this.render(); return; }
    this.state = 'loading-journal'; this.render();
    try {
      const journalId = await getOrCreateSandboxJournalId(this.runtime.firestore, user.uid);
      if (generation !== this.generation || !this.runtime) return;
      this.transport = new FirestoreSandboxTransport(this.runtime.firestore, user.uid, journalId);
      this.state = 'signed-in';
    } catch (failure) {
      if (generation !== this.generation) return;
      this.state = 'failed'; this.failure = syncFailure(failure);
    }
    this.render();
  }

  private async loadFixtureChanges(): Promise<Z5Change[]> {
    if (!this.changes.length) {
      const batch = await parseCanonicalBatch(fixtureJson.replace(/\n$/, ''));
      if (batch.changes.length !== 2 || batch.changes[0].operation !== 'create' || batch.changes[1].operation !== 'delete') throw new Error('Invalid test fixture.');
      this.changes = batch.changes;
    }
    return this.changes;
  }
  private async perform(action: (transport: FirestoreSandboxTransport) => Promise<void>): Promise<void> {
    if (!this.transport || this.busy) return;
    this.busy = true; this.failure = undefined; this.render();
    try { await action(this.transport); }
    catch (failure) { this.successful = false; this.failure = syncFailure(failure); this.developerError.textContent = firebaseErrorMessage(failure); }
    finally { this.busy = false; await this.renderConflicts(); }
  }
  private render(): void {
    const state = this.viewState();
    this.authState.textContent = syncLabel(state);
    this.account.textContent = this.user?.email ? `Google account: ${this.user.email}` : state.connected ? 'Connected with Google' : '';
    this.account.hidden = !this.account.textContent;
    this.status.textContent = this.notice ?? (this.lastSync ? `Last synced: ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(this.lastSync)}` : 'Never synced');
    this.error.textContent = syncMessage(state);
    this.error.hidden = !this.error.textContent;
    const active = document.activeElement instanceof HTMLButtonElement && this.controls.contains(document.activeElement) ? document.activeElement.textContent : null;
    this.controls.replaceChildren();
    if (this.user && this.failure !== 'auth' && this.failure !== 'cancelled') {
      const sync = actionButton('Sync now', () => { void this.syncEntries(); }, 'primary sandbox-wide');
      sync.disabled = this.busy || this.state !== 'signed-in' || !this.transport;
      const disconnect = actionButton('Disconnect', () => { void this.disconnect(); }, 'sandbox-wide');
      disconnect.disabled = this.busy;
      this.controls.append(sync, disconnect);
    } else {
      const connect = actionButton('Connect with Google', () => { void this.beginSignIn(); }, 'primary sandbox-wide');
      connect.disabled = this.busy || !this.runtime;
      this.controls.append(connect);
      if (this.user) {
        const disconnect = actionButton('Disconnect', () => { void this.disconnect(); }, 'sandbox-wide');
        disconnect.disabled = this.busy;
        this.controls.append(disconnect);
      }
    }
    if (this.conflictCount) {
      const review = actionButton('Review conflict', () => { this.reviewing = !this.reviewing; void this.renderConflicts(); }, 'sandbox-wide');
      review.disabled = this.busy; this.controls.append(review);
    }
    if (active) Array.from(this.controls.querySelectorAll('button')).find((button) => button.textContent === active && !button.disabled)?.focus();
    this.developerControls.replaceChildren();
    if (this.user && this.transport) {
      for (const button of [
        actionButton('Upload Change A · synthetic create', () => { void this.uploadFixture(0); }),
        actionButton('Upload Change B · synthetic tombstone', () => { void this.uploadFixture(1); }),
        actionButton('Pull sandbox packets', () => { void this.pull(); }),
        actionButton('Try same-ID collision', () => { void this.tryCollision(); }),
      ]) { button.disabled = this.busy || this.state !== 'signed-in'; this.developerControls.append(button); }
      const collision = this.developerControls.lastElementChild as HTMLButtonElement;
      collision.disabled ||= !this.changes[0] || !this.packets.some((packet) => packet.change.change_id === this.changes[0].change_id);
    }
    this.packetList.replaceChildren();
    for (const packet of this.packets) this.packetList.append(element('li', `${packet.change.change_id} · rev ${packet.change.revision} · ${packet.change.operation}`));
  }

  private async syncEntries(): Promise<void> {
    await this.perform(async (transport) => {
      if (!this.store || !this.user) throw new Error('Journal unavailable.');
      const provider = `firebase-sandbox:${transport.journalId}`;
      this.notice = undefined;
      await syncDisposableEntries(this.store, provider, transport);
      await this.refreshToday?.(); await this.renderConflicts();
      this.successful = (await this.store.pending(provider)).length === 0;
      this.lastSync = new Date();
    });
  }
  private async disconnect(): Promise<void> {
    if (!this.runtime || this.busy) return;
    this.busy = true; this.operation = 'disconnect'; this.render();
    try { await signOutSandbox(this.runtime.auth); }
    catch (failure) { this.failure = syncFailure(failure); }
    finally { this.busy = false; this.operation = undefined; this.render(); }
  }
  private async beginSignIn(): Promise<void> {
    if (!this.runtime || this.busy) return;
    this.busy = true; this.connecting = true; this.failure = undefined; this.developerError.textContent = ''; this.render();
    try { await signInWithGoogle(this.runtime.auth, standaloneDisplay()); }
    catch (failure) {
      this.failure = syncFailure(failure);
      this.developerError.textContent = `${firebaseErrorMessage(failure)} ${formatSafeFirebaseAuthErrorShape(failure)}`;
    } finally { this.busy = false; this.connecting = false; this.render(); }
  }
  private async uploadFixture(index: number): Promise<void> {
    await this.perform(async (transport) => {
      const changes = await this.loadFixtureChanges();
      const result = await transport.uploadCanonicalChange(canonicalJson(changes[index]));
      this.developerStatus.textContent = result === 'uploaded' ? `Uploaded synthetic Change ${index === 0 ? 'A' : 'B'}.` : 'This exact synthetic Change was already uploaded.';
    });
  }
  private async pull(): Promise<void> {
    await this.perform(async (transport) => { this.packets = await transport.pullChanges(); this.developerStatus.textContent = `Pulled ${this.packets.length} sandbox packets.`; });
  }
  private async tryCollision(): Promise<void> {
    await this.perform(async (transport) => {
      const [first] = await this.loadFixtureChanges();
      try { await transport.uploadCanonicalChange(await makeSyntheticCollisionPayload(first)); this.developerStatus.textContent = 'Collision unexpectedly accepted.'; }
      catch (failure) {
        if (failure instanceof SandboxIdentityCollisionError) { this.developerStatus.textContent = 'Same-ID collision rejected.'; return; }
        throw failure;
      }
    });
  }
}
