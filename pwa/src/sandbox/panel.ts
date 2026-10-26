import { onAuthStateChanged, type User } from 'firebase/auth';
import fixtureJson from './synthetic-v1.json?raw';
import { completeGoogleRedirect, firebaseErrorMessage, formatSafeFirebaseAuthErrorShape, initializeFirebaseRuntime,
  signInWithGoogle, signOutSandbox, type FirebaseEnvironment, type FirebaseRuntime } from './firebase';
import { getOrCreateSandboxJournalId, FirestoreSandboxTransport, SandboxIdentityCollisionError,
  type PulledSandboxChange } from './transport';
import { canonicalJson, makeSyntheticCollisionPayload, parseCanonicalBatch, type Z5Change } from './z5-wire';

type PanelState = 'checking' | 'unconfigured' | 'signed-out' | 'loading-journal' | 'signed-in' | 'failed';

function element<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actionButton(text: string, callback: () => void, className = ''): HTMLButtonElement {
  const button = element('button', text, className);
  button.type = 'button';
  button.addEventListener('click', callback);
  return button;
}

function standaloneDisplay(): boolean {
  const legacyNavigator = navigator as Navigator & { standalone?: boolean };
  return window.matchMedia('(display-mode: standalone)').matches || legacyNavigator.standalone === true;
}

export class SandboxSyncPanel {
  readonly element = element('details', undefined, 'sandbox-panel');
  private readonly summary = element('summary', 'Sandbox Sync · developer proof');
  private readonly intro = element('p', 'Testing only. Cloud actions use the checked-in synthetic Z5 fixture. Normal entries stay in this browser and are never uploaded.', 'sandbox-copy');
  private readonly authState = element('p', 'Checking Firebase configuration…', 'sandbox-state');
  private readonly status = element('p', '', 'sandbox-state');
  private readonly error = element('p', '', 'error-message');
  private readonly controls = element('div', undefined, 'sandbox-actions');
  private readonly packetList = element('ul', undefined, 'sandbox-packets');
  private runtime?: FirebaseRuntime;
  private transport?: FirestoreSandboxTransport;
  private user: User | null = null;
  private state: PanelState = 'checking';
  private packets: PulledSandboxChange[] = [];
  private changes: Z5Change[] = [];
  private busy = false;
  private generation = 0;
  private unsubscribe?: () => void;

  constructor() {
    this.summary.id = 'sandbox-sync-heading';
    this.authState.setAttribute('role', 'status');
    this.status.setAttribute('role', 'status');
    this.error.setAttribute('role', 'alert');
    this.packetList.setAttribute('aria-label', 'Pulled sandbox packet IDs');
    this.element.append(this.summary, this.intro, this.authState, this.status, this.error, this.controls, this.packetList);
    this.render();
  }

  async start(environment: FirebaseEnvironment): Promise<void> {
    try {
      const runtime = await initializeFirebaseRuntime(environment);
      if (!runtime) {
        this.state = 'unconfigured';
        this.authState.textContent = 'Firebase Sandbox Sync is not configured. Local logging works without it.';
        this.status.textContent = 'Copy pwa/.env.example to pwa/.env.local to enable this test panel.';
        this.render();
        return;
      }
      this.runtime = runtime;
      this.state = 'checking';
      this.authState.textContent = 'Checking Google sign-in status…';
      this.unsubscribe = onAuthStateChanged(runtime.auth, (user) => { void this.onUserChanged(user); }, (failure) => {
        this.state = 'failed';
        this.error.textContent = firebaseErrorMessage(failure);
        this.render();
      });
      await completeGoogleRedirect(runtime.auth);
    } catch (failure) {
      this.state = 'failed';
      this.authState.textContent = 'Sandbox Sync could not initialize. Local logging remains available.';
      this.error.textContent = firebaseErrorMessage(failure);
    }
    this.render();
  }

  dispose(): void { this.unsubscribe?.(); }

  private async onUserChanged(user: User | null): Promise<void> {
    const generation = ++this.generation;
    this.user = user;
    this.transport = undefined;
    this.packets = [];
    this.error.textContent = '';
    this.status.textContent = '';
    if (!user || !this.runtime) {
      this.state = 'signed-out';
      this.authState.textContent = 'Signed out. Local logging does not require an account.';
      this.render();
      return;
    }
    this.state = 'loading-journal';
    this.authState.textContent = 'Signed in. Opening this account’s sandbox journal…';
    this.render();
    try {
      const journalId = await getOrCreateSandboxJournalId(this.runtime.firestore, user.uid);
      if (generation !== this.generation || !this.runtime) return;
      this.transport = new FirestoreSandboxTransport(this.runtime.firestore, user.uid, journalId);
      this.state = 'signed-in';
      this.authState.textContent = 'Signed in to Sandbox Sync.';
      this.status.textContent = `Opaque sandbox journal ID: ${journalId}`;
    } catch (failure) {
      if (generation !== this.generation) return;
      this.state = 'failed';
      this.authState.textContent = 'Signed in, but the sandbox journal is unavailable.';
      this.error.textContent = firebaseErrorMessage(failure);
    }
    this.render();
  }

  private async loadFixtureChanges(): Promise<Z5Change[]> {
    if (!this.changes.length) {
      const batch = await parseCanonicalBatch(fixtureJson.replace(/\n$/, ''));
      if (batch.changes.length !== 2 || batch.changes[0].operation !== 'create' || batch.changes[1].operation !== 'delete') {
        throw new Error('The desktop-generated sandbox fixture does not have the expected create/tombstone pair.');
      }
      this.changes = batch.changes;
    }
    return this.changes;
  }

  private async perform(action: (transport: FirestoreSandboxTransport) => Promise<void>): Promise<void> {
    if (!this.transport || this.busy) return;
    this.busy = true;
    this.error.textContent = '';
    this.render();
    try { await action(this.transport); }
    catch (failure) { this.error.textContent = firebaseErrorMessage(failure); }
    finally { this.busy = false; this.render(); }
  }

  private render(): void {
    this.controls.replaceChildren();
    const configured = this.runtime !== undefined;
    if (this.user && this.runtime) {
      this.controls.append(actionButton('Sign out', () => { void this.perform(async () => signOutSandbox(this.runtime!.auth)); }));
      const canUseTransport = this.state === 'signed-in' && this.transport !== undefined;
      this.controls.append(
        actionButton('Upload Change A · synthetic create', () => { void this.uploadFixture(0); }, 'sandbox-wide'),
        actionButton('Upload Change B · synthetic tombstone', () => { void this.uploadFixture(1); }, 'sandbox-wide'),
        actionButton('Pull sandbox packets', () => { void this.pull(); }, 'sandbox-wide'),
        actionButton('Try same-ID collision', () => { void this.tryCollision(); }, 'sandbox-wide'),
      );
      for (const control of Array.from(this.controls.querySelectorAll('button'))) control.disabled = this.busy || !canUseTransport;
      const fixtureId = this.changes[0]?.change_id;
      const hasFixture = fixtureId !== undefined && this.packets.some((packet) => packet.change.change_id === fixtureId);
      const collisionButton = Array.from(this.controls.querySelectorAll('button')).find((control) => control.textContent === 'Try same-ID collision');
      if (collisionButton) collisionButton.disabled ||= !hasFixture;
    } else {
      const signIn = actionButton('Sign in with Google', () => { void this.beginSignIn(); }, 'sandbox-wide');
      signIn.disabled = this.busy || !configured || !this.runtime;
      this.controls.append(signIn);
    }
    this.packetList.replaceChildren();
    for (const packet of this.packets) {
      const row = element('li');
      row.append(element('code', packet.change.change_id), document.createTextNode(` · rev ${packet.change.revision} · ${packet.change.operation} · received`));
      this.packetList.append(row);
    }
    if (this.user && this.packets.length === 0 && this.state === 'signed-in') {
      this.packetList.append(element('li', 'No packets pulled yet.'));
    }
  }

  private async beginSignIn(): Promise<void> {
    if (!this.runtime || this.busy) return;
    this.busy = true;
    this.error.textContent = '';
    this.render();
    try { await signInWithGoogle(this.runtime.auth, standaloneDisplay()); }
    catch (failure) {
      this.error.textContent = `${firebaseErrorMessage(failure)} ${formatSafeFirebaseAuthErrorShape(failure)}`;
    }
    finally { this.busy = false; this.render(); }
  }

  private async uploadFixture(index: number): Promise<void> {
    await this.perform(async (transport) => {
      const changes = await this.loadFixtureChanges();
      const result = await transport.uploadCanonicalChange(canonicalJson(changes[index]));
      this.status.textContent = result === 'uploaded' ? `Uploaded synthetic Change ${index === 0 ? 'A' : 'B'}.` : 'This exact synthetic Change was already uploaded; delivery is idempotent.';
    });
  }

  private async pull(): Promise<void> {
    await this.perform(async (transport) => {
      this.packets = await transport.pullChanges();
      this.status.textContent = `Pulled ${this.packets.length} validated sandbox packet${this.packets.length === 1 ? '' : 's'}. No packet was applied to local entries.`;
    });
  }

  private async tryCollision(): Promise<void> {
    await this.perform(async (transport) => {
      const [first] = await this.loadFixtureChanges();
      const alteredPayload = await makeSyntheticCollisionPayload(first);
      try {
        await transport.uploadCanonicalChange(alteredPayload);
        this.status.textContent = 'The identity collision check unexpectedly uploaded a second payload.';
      } catch (failure) {
        if (failure instanceof SandboxIdentityCollisionError) {
          this.status.textContent = 'Same-ID / different-payload collision rejected. The original cloud packet is unchanged.';
          return;
        }
        throw failure;
      }
    });
  }
}
