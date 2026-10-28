import { type Auth, type User } from 'firebase/auth';
import fixtureJson from './synthetic-v1.json?raw';
import { completeGoogleRedirect, firebaseErrorMessage, formatSafeFirebaseAuthErrorShape, initializeFirebaseRuntime,
  observeSandboxUser, signInWithGoogle, signOutSandbox, type FirebaseEnvironment, type FirebaseRuntime } from './firebase';
import { discoverProductionJournals, getOrCreateSandboxJournalId, FirestoreSandboxTransport, SandboxIdentityCollisionError,
  verifyProductionJournalDescriptor, type ProductionJournalDescriptor, type PulledSandboxChange } from './transport';
import { canonicalJson, makeSyntheticCollisionPayload, parseCanonicalBatch, type Z5Change } from './z5-wire';
import type { EpisodeStore } from '../storage';
import { joinProductionJournal, productionProvider, syncDisposableEntries, syncProductionEntries, type DisposableMailbox } from './disposable-sync';
import { candidateSummary, conflictExplanation, syncFailure, syncLabel, syncMessage, type SyncFailure, type SyncViewState } from './presentation';

type PanelState = 'checking' | 'unconfigured' | 'signed-out' | 'loading-journal' | 'signed-in' | 'failed';
type ProductionPanelState = 'checking' | 'unconfigured' | 'signed-out' | 'loading-journal' | 'awaiting-join' | 'no-journal' | 'signed-in' | 'failed';

export interface SyncAuthLifecycleDeps {
  initRuntime?: (env: FirebaseEnvironment) => Promise<FirebaseRuntime | undefined>;
  observeAuth?: (auth: Auth, next: (user: User | null) => void, failure: (error: Error) => void) => () => void;
  completeRedirect?: (auth: Auth) => Promise<User | null>;
  signIn?: (auth: Auth, standalone: boolean) => Promise<User | null>;
  signOut?: (auth?: Auth) => Promise<void>;
  isStandalone?: () => boolean;
}

export interface SandboxSyncServiceDeps extends SyncAuthLifecycleDeps {
  getSandboxJournalId?: (uid: string) => Promise<string>;
  createTransport?: (uid: string, journalId: string) => FirestoreSandboxTransport;
}

export interface ProductionSyncServiceDeps extends SyncAuthLifecycleDeps {
  discoverJournals?: (uid: string) => Promise<ProductionJournalDescriptor[]>;
  verifyDescriptor?: (uid: string, journalId: string) => Promise<ProductionJournalDescriptor>;
  createMailbox?: (uid: string, journalId: string) => DisposableMailbox;
}

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

  constructor(private readonly deps: SandboxSyncServiceDeps = {}) {
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
      const init = this.deps.initRuntime ?? initializeFirebaseRuntime;
      const runtime = await init(environment);
      if (!runtime) { this.state = 'unconfigured'; this.render(); return; }
      this.runtime = runtime;
      const observe = this.deps.observeAuth ?? observeSandboxUser;
      const complete = this.deps.completeRedirect ?? completeGoogleRedirect;
      let startupRedirectUser: User | null = null;
      let startupSettled = false;
      this.unsubscribe = observe(runtime.auth, (user) => {
        if (!user && !startupSettled && startupRedirectUser) return;
        void this.onUserChanged(user);
      }, (failure) => {
        this.state = 'failed';
        this.failure = syncFailure(failure);
        this.developerError.textContent = `${firebaseErrorMessage(failure)} ${formatSafeFirebaseAuthErrorShape(failure)}`;
        this.render();
      });
      startupRedirectUser = await complete(runtime.auth);
      if (startupRedirectUser && (this.user?.uid !== startupRedirectUser.uid || (this.state !== 'loading-journal' && this.state !== 'signed-in'))) {
        await this.onUserChanged(startupRedirectUser);
      } else if (!this.user && runtime.auth.currentUser) {
        await this.onUserChanged(runtime.auth.currentUser);
      } else if (!this.user && this.state === 'checking') {
        this.state = 'signed-out';
      }
      startupSettled = true;
    } catch (failure) {
      this.state = 'failed';
      this.failure = syncFailure(failure);
      this.developerError.textContent = `${firebaseErrorMessage(failure)} ${formatSafeFirebaseAuthErrorShape(failure)}`;
    }
    this.render();
  }
  dispose(): void { this.unsubscribe?.(); }

  private async onUserChanged(user: User | null): Promise<void> {
    if (user && this.user?.uid === user.uid && (this.state === 'loading-journal' || this.state === 'signed-in')) {
      this.user = user;
      this.render();
      return;
    }
    const generation = ++this.generation;
    const hadUser = this.user !== null;
    if (this.user?.uid !== user?.uid) { this.lastSync = undefined; this.successful = false; }
    this.user = user; this.transport = undefined; this.packets = []; this.notice = undefined;
    this.developerStatus.textContent = '';
    if (!user || !this.runtime) {
      if (hadUser || this.state !== 'failed') {
        this.failure = undefined;
        this.developerError.textContent = '';
        this.state = 'signed-out';
      }
      this.render();
      return;
    }
    this.failure = undefined;
    this.developerError.textContent = '';
    this.state = 'loading-journal'; this.render();
    try {
      const journalId = this.deps.getSandboxJournalId
        ? await this.deps.getSandboxJournalId(user.uid)
        : await getOrCreateSandboxJournalId(this.runtime.firestore, user.uid);
      if (generation !== this.generation || !this.runtime) return;
      this.transport = this.deps.createTransport
        ? this.deps.createTransport(user.uid, journalId)
        : new FirestoreSandboxTransport(this.runtime.firestore, user.uid, journalId);
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
    try {
      const signOut = this.deps.signOut ?? signOutSandbox;
      await signOut(this.runtime.auth);
    }
    catch (failure) { this.failure = syncFailure(failure); }
    finally { this.busy = false; this.operation = undefined; this.render(); }
  }
  private async beginSignIn(): Promise<void> {
    if (!this.runtime || this.busy) return;
    this.busy = true; this.connecting = true; this.failure = undefined; this.developerError.textContent = ''; this.render();
    try {
      const signIn = this.deps.signIn ?? signInWithGoogle;
      const isStandalone = this.deps.isStandalone ?? standaloneDisplay;
      const user = await signIn(this.runtime.auth, isStandalone());
      if (user && (this.user?.uid !== user.uid || (this.state !== 'loading-journal' && this.state !== 'signed-in'))) {
        await this.onUserChanged(user);
      }
    }
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

export class ProductionSyncPanel {
  readonly element = element('details', undefined, 'production-panel');
  private readonly summary = element('summary', 'Sync · Production Journal');
  private readonly intro = element('p', 'Join your existing desktop production journal with Google. Disposable sandbox entries are kept separate and are never uploaded.', 'sandbox-copy');
  private readonly authState = element('p', '', 'sandbox-state sync-heading');
  private readonly account = element('p', '', 'sandbox-copy');
  private readonly status = element('p', '', 'sandbox-state');
  private readonly discoveryBox = element('div', undefined, 'production-discovery');
  private readonly error = element('p', '', 'error-message');
  private readonly controls = element('div', undefined, 'sandbox-actions');
  private readonly conflictList = element('div', undefined, 'sandbox-conflicts');
  private runtime?: FirebaseRuntime;
  private mailbox?: DisposableMailbox;
  private user: User | null = null;
  private state: ProductionPanelState = 'checking';
  private joinedJournalId: string | null = null;
  private discovered: ProductionJournalDescriptor[] = [];
  private selectedJournalId: string | null = null;
  private busy = false;
  private connecting = false;
  private operation?: 'disconnect' | 'resolving';
  private successful = false;
  private failure?: SyncFailure;
  private authError?: string;
  private lastSync?: Date;
  private notice?: string;
  private conflictCount = 0;
  private reviewing = false;
  private generation = 0;
  private conflictGeneration = 0;
  private unsubscribe?: () => void;
  private store?: EpisodeStore;
  private onActivated?: () => Promise<void>;
  private userChangeTask: Promise<void> = Promise.resolve();

  constructor(private readonly deps: ProductionSyncServiceDeps = {}) {
    this.summary.id = 'production-sync-heading';
    this.authState.setAttribute('role', 'status');
    this.status.setAttribute('role', 'status');
    this.error.setAttribute('role', 'alert');
    this.element.addEventListener('toggle', () => {
      if (this.element.open && this.user && !this.busy && this.state === 'signed-out') {
        void this.inspectAccount(this.user);
      }
    });
    this.element.append(this.summary, this.intro, this.authState, this.account, this.status, this.discoveryBox, this.error, this.controls, this.conflictList);
    this.render();
  }

  async attachStore(store: EpisodeStore, onActivated: () => Promise<void>): Promise<void> {
    this.store = store;
    this.onActivated = onActivated;
    this.joinedJournalId = await store.journalId();
    const savedSync = await store.lastSyncedAt();
    if (savedSync) this.lastSync = new Date(savedSync);
    if (this.joinedJournalId) await this.onActivated();
    await this.renderConflicts();
  }

  localChanged(): void { this.successful = false; this.notice = undefined; this.render(); }

  private viewState(): SyncViewState {
    const connected = (this.state === 'signed-in' || this.state === 'awaiting-join' || this.state === 'no-journal') && !!this.user;
    return {
      connected,
      busy: this.busy,
      connecting: this.connecting || this.state === 'checking' || this.state === 'loading-journal',
      operation: this.operation,
      successful: this.state === 'signed-in' && this.successful,
      conflicts: this.conflictCount,
      failure: this.failure,
    };
  }

  async start(environment: FirebaseEnvironment): Promise<void> {
    try {
      const init = this.deps.initRuntime ?? initializeFirebaseRuntime;
      const runtime = await init(environment);
      if (!runtime) { this.state = 'unconfigured'; this.render(); return; }
      this.runtime = runtime;
      const observe = this.deps.observeAuth ?? observeSandboxUser;
      const complete = this.deps.completeRedirect ?? completeGoogleRedirect;
      let startupRedirectUser: User | null = null;
      let startupSettled = false;
      this.unsubscribe = observe(runtime.auth, (user) => {
        if (!user && !startupSettled && startupRedirectUser) return;
        void this.onUserChanged(user);
      }, (failure) => {
        this.state = 'failed';
        this.failure = syncFailure(failure);
        this.authError = firebaseErrorMessage(failure);
        this.render();
      });
      startupRedirectUser = await complete(runtime.auth);
      if (startupRedirectUser) {
        await this.onUserChanged(startupRedirectUser);
      } else if (this.user) {
        await this.userChangeTask;
      } else if (runtime.auth.currentUser) {
        await this.onUserChanged(runtime.auth.currentUser);
      } else if (this.state === 'checking') {
        this.state = 'signed-out';
      }
      startupSettled = true;
    } catch (failure) {
      this.state = 'failed';
      this.failure = syncFailure(failure);
      this.authError = firebaseErrorMessage(failure);
    }
    this.render();
  }

  dispose(): void { this.unsubscribe?.(); }

  async onUserChanged(user: User | null): Promise<void> {
    const activeStates: ProductionPanelState[] = ['loading-journal', 'awaiting-join', 'no-journal', 'signed-in'];
    if (user && this.user?.uid === user.uid && activeStates.includes(this.state)) {
      this.user = user;
      this.render();
      await this.userChangeTask;
      return;
    }
    const generation = ++this.generation;
    const hadUser = this.user !== null;
    if (this.user?.uid !== user?.uid) { this.successful = false; }
    this.user = user;
    this.mailbox = undefined;
    this.discovered = [];
    this.selectedJournalId = null;
    if (!user) {
      this.userChangeTask = Promise.resolve();
      if (hadUser || this.state !== 'failed') {
        this.failure = undefined;
        this.authError = undefined;
        this.state = 'signed-out';
      }
      this.render();
      return;
    }
    this.failure = undefined;
    this.authError = undefined;
    const task = this.inspectAccount(user, generation);
    this.userChangeTask = task;
    await task;
  }

  async inspectAccount(user: User, generation = ++this.generation): Promise<void> {
    this.user = user;
    this.state = 'loading-journal';
    this.failure = undefined;
    this.authError = undefined;
    this.notice = undefined;
    this.render();
    try {
      if (this.joinedJournalId !== null) {
        await this.verifyDescriptor(user.uid, this.joinedJournalId);
        if (generation !== this.generation) return;
        this.mailbox = this.buildMailbox(user.uid, this.joinedJournalId);
        this.state = 'signed-in';
        const pending = this.store ? await this.store.pending(productionProvider(this.joinedJournalId)) : [];
        this.successful = Boolean(this.lastSync) && pending.length === 0 && this.conflictCount === 0;
      } else {
        const journals = await this.discoverJournals(user.uid);
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
    this.render();
  }

  private async discoverJournals(uid: string): Promise<ProductionJournalDescriptor[]> {
    if (this.deps.discoverJournals) return this.deps.discoverJournals(uid);
    if (!this.runtime) throw new Error('Firebase runtime unavailable.');
    return discoverProductionJournals(this.runtime.firestore, uid);
  }

  private async verifyDescriptor(uid: string, journalId: string): Promise<ProductionJournalDescriptor> {
    if (this.deps.verifyDescriptor) return this.deps.verifyDescriptor(uid, journalId);
    if (!this.runtime) throw new Error('Firebase runtime unavailable.');
    return verifyProductionJournalDescriptor(this.runtime.firestore, uid, journalId);
  }

  private buildMailbox(uid: string, journalId: string): DisposableMailbox {
    if (this.deps.createMailbox) return this.deps.createMailbox(uid, journalId);
    if (!this.runtime) throw new Error('Firebase runtime unavailable.');
    return new FirestoreSandboxTransport(this.runtime.firestore, uid, journalId);
  }

  selectJournal(journalId: string): void {
    if (!this.discovered.some((descriptor) => descriptor.journalId === journalId)) return;
    this.selectedJournalId = journalId;
    this.render();
  }

  async confirmJoin(): Promise<void> {
    if (this.busy || this.state !== 'awaiting-join' || !this.selectedJournalId || !this.store || !this.user) return;
    const targetJournalId = this.selectedJournalId;
    this.busy = true;
    this.failure = undefined;
    this.render();
    try {
      await this.verifyDescriptor(this.user.uid, targetJournalId);
      const mailbox = this.buildMailbox(this.user.uid, targetJournalId);
      await joinProductionJournal(this.store, targetJournalId, mailbox);
      this.joinedJournalId = targetJournalId;
      this.mailbox = mailbox;
      this.discovered = [];
      this.state = 'signed-in';
      this.lastSync = new Date();
      await this.store.setLastSyncedAt(this.lastSync.toISOString());
      await this.onActivated?.();
      await this.renderConflicts();
      const provider = productionProvider(targetJournalId);
      this.successful = (await this.store.pending(provider)).length === 0 && this.conflictCount === 0;
      this.notice = undefined;
    } catch (failure) {
      this.successful = false;
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      await this.renderConflicts();
    }
  }

  async syncEntries(): Promise<void> {
    if (this.busy || this.state !== 'signed-in' || !this.mailbox || !this.joinedJournalId || !this.store || !this.user) return;
    this.busy = true;
    this.failure = undefined;
    this.render();
    try {
      await this.verifyDescriptor(this.user.uid, this.joinedJournalId);
      this.notice = undefined;
      await syncProductionEntries(this.store, this.joinedJournalId, this.mailbox);
      await this.onActivated?.();
      await this.renderConflicts();
      const provider = productionProvider(this.joinedJournalId);
      this.successful = (await this.store.pending(provider)).length === 0 && this.conflictCount === 0;
      this.lastSync = new Date();
      await this.store.setLastSyncedAt(this.lastSync.toISOString());
    } catch (failure) {
      this.successful = false;
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      await this.renderConflicts();
    }
  }

  async disconnect(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.operation = 'disconnect';
    this.render();
    try {
      if (this.deps.signOut) await this.deps.signOut(this.runtime?.auth);
      else if (this.runtime) await signOutSandbox(this.runtime.auth);
      this.user = null;
      this.mailbox = undefined;
      this.discovered = [];
      this.selectedJournalId = null;
      this.state = 'signed-out';
      this.successful = false;
      this.failure = undefined;
      this.authError = undefined;
      if (this.joinedJournalId) {
        this.notice = 'Disconnected. Your production journal and unsynced changes remain on this device.';
      }
    } catch (failure) {
      this.failure = syncFailure(failure);
    } finally {
      this.busy = false;
      this.operation = undefined;
      this.render();
    }
  }

  async beginSignIn(): Promise<void> {
    if (this.busy) return;
    if (this.user) {
      await this.inspectAccount(this.user);
      return;
    }
    if (!this.runtime) return;
    this.busy = true;
    this.connecting = true;
    this.failure = undefined;
    this.authError = undefined;
    this.render();
    try {
      const signIn = this.deps.signIn ?? signInWithGoogle;
      const isStandalone = this.deps.isStandalone ?? standaloneDisplay;
      const user = await signIn(this.runtime.auth, isStandalone());
      if (user) {
        await this.onUserChanged(user);
      } else {
        await this.userChangeTask;
      }
    } catch (failure) {
      this.state = 'failed';
      this.failure = syncFailure(failure);
      this.authError = firebaseErrorMessage(failure);
    } finally {
      this.busy = false;
      this.connecting = false;
      this.render();
    }
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
      await this.onActivated?.();
      await this.renderConflicts();
      this.notice = 'Version saved. Sync now to share it.';
    } catch (failure) { this.failure = syncFailure(failure); }
    finally { this.busy = false; this.operation = undefined; await this.renderConflicts(); }
  }

  private formatDescriptorSummary(descriptor: ProductionJournalDescriptor): string {
    const suffix = descriptor.journalId.slice(-6);
    const date = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(descriptor.publishedAt.toDate());
    return `Journal …${suffix} · Published ${date}`;
  }

  private render(): void {
    const state = this.viewState();
    this.authState.textContent = syncLabel(state);
    this.account.textContent = this.user?.email ? `Connected with Google · ${this.user.email}` : state.connected ? 'Connected with Google' : '';
    this.account.hidden = !this.account.textContent;
    this.status.textContent = this.notice ?? (this.lastSync ? `Last synced: ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(this.lastSync)}` : 'Never synced');
    const baseMessage = syncMessage(state);
    this.error.textContent = this.authError ? `${baseMessage} ${this.authError}`.trim() : baseMessage;
    this.error.hidden = !this.error.textContent;

    this.discoveryBox.replaceChildren();
    if (this.state === 'awaiting-join' && this.discovered.length === 1) {
      const card = element('div', undefined, 'sync-version');
      card.append(
        element('h4', this.formatDescriptorSummary(this.discovered[0])),
        element('p', 'Your existing desktop entries will be downloaded into isolated production storage on this device. Disposable sandbox entries will not be uploaded.', 'sandbox-copy'),
      );
      this.discoveryBox.append(card);
    } else if (this.state === 'awaiting-join' && this.discovered.length > 1) {
      const fieldset = element('fieldset', undefined, 'tag-field');
      fieldset.append(element('legend', 'Choose production journal'));
      const options = element('div', undefined, 'tag-options');
      for (const descriptor of this.discovered) {
        const label = element('label');
        const radio = element('input');
        radio.type = 'radio';
        radio.name = 'production-journal-choice';
        radio.value = descriptor.journalId;
        radio.checked = this.selectedJournalId === descriptor.journalId;
        radio.disabled = this.busy;
        radio.addEventListener('change', () => {
          if (radio.checked) this.selectJournal(descriptor.journalId);
        });
        label.append(radio, element('span', this.formatDescriptorSummary(descriptor)));
        options.append(label);
      }
      fieldset.append(options);
      this.discoveryBox.append(fieldset);
    }

    this.controls.replaceChildren();
    const canSignIn = Boolean(this.runtime || this.deps.discoverJournals || this.user);
    if (this.user && this.failure !== 'auth' && this.failure !== 'cancelled' && this.state !== 'signed-out') {
      if (this.state === 'awaiting-join') {
        const joinBtn = actionButton('Join existing journal', () => { void this.confirmJoin(); }, 'primary sandbox-wide');
        joinBtn.disabled = this.busy || !this.selectedJournalId;
        this.controls.append(joinBtn);
      } else if (this.state === 'signed-in' && this.joinedJournalId) {
        const syncBtn = actionButton('Sync now', () => { void this.syncEntries(); }, 'primary sandbox-wide');
        syncBtn.disabled = this.busy || !this.mailbox;
        this.controls.append(syncBtn);
      }
      const disconnect = actionButton('Disconnect', () => { void this.disconnect(); }, 'sandbox-wide');
      disconnect.disabled = this.busy;
      this.controls.append(disconnect);
    } else {
      const connect = actionButton('Connect with Google', () => { void this.beginSignIn(); }, 'primary sandbox-wide');
      connect.disabled = this.busy || !canSignIn;
      this.controls.append(connect);
      if (this.user) {
        const disconnect = actionButton('Disconnect', () => { void this.disconnect(); }, 'sandbox-wide');
        disconnect.disabled = this.busy;
        this.controls.append(disconnect);
      }
    }
    if (this.conflictCount && this.joinedJournalId) {
      const review = actionButton('Review conflict', () => { this.reviewing = !this.reviewing; void this.renderConflicts(); }, 'sandbox-wide');
      review.disabled = this.busy;
      this.controls.append(review);
    }
  }
}
