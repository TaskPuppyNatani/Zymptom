import 'fake-indexeddb/auto';
import { FirebaseError } from 'firebase/app';
import type { Auth, User } from 'firebase/auth';
import { Timestamp, type Firestore } from 'firebase/firestore';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { blankEpisode, type EpisodeInput } from '../../src/model';
import {
  joinProductionJournal,
  ProductionSyncController,
  productionProvider,
  syncProductionEntries,
} from '../../src/sandbox/disposable-sync';
import { completeGoogleRedirect, type FirebaseRuntime } from '../../src/sandbox/firebase';
import { ProductionSyncPanel, SandboxSyncPanel } from '../../src/sandbox/panel';
import { FirestoreSandboxTransport, parseProductionJournalDescriptor, type ProductionJournalDescriptor } from '../../src/sandbox/transport';
import { canonicalJson, parseCanonicalChange } from '../../src/sandbox/z5-wire';
import { EpisodeStore } from '../../src/storage';
import fixture from '../fixtures/z5-v1.json';

class FakeElement {
  tagName: string;
  textContent = '';
  className = '';
  id = '';
  hidden = false;
  disabled = false;
  type = '';
  name = '';
  value = '';
  checked = false;
  open = false;
  children: FakeElement[] = [];
  private attrs = new Map<string, string>();
  private listeners = new Map<string, Array<() => void>>();

  constructor(tag: string) {
    this.tagName = tag.toUpperCase();
  }

  setAttribute(k: string, v: string): void {
    this.attrs.set(k, v);
  }

  getAttribute(k: string): string | null {
    return this.attrs.get(k) ?? null;
  }

  addEventListener(event: string, cb: () => void): void {
    const list = this.listeners.get(event) ?? [];
    list.push(cb);
    this.listeners.set(event, list);
  }

  dispatchEvent(event: string): void {
    for (const cb of this.listeners.get(event) ?? []) cb();
  }

  click(): void {
    if (this.disabled) return;
    this.dispatchEvent('click');
  }

  append(...nodes: FakeElement[]): void {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes: FakeElement[]): void {
    this.children = [...nodes];
  }

  contains(target: unknown): boolean {
    if (target === this) return true;
    return this.children.some((c) => c.contains(target));
  }

  get lastElementChild(): FakeElement | null {
    return this.children.at(-1) ?? null;
  }

  querySelectorAll(selector: string): FakeElement[] {
    const tag = selector.toUpperCase();
    const out: FakeElement[] = [];
    const visit = (node: FakeElement) => {
      for (const child of node.children) {
        if (child.tagName === tag || child.className.split(/\s+/).includes(selector.replace(/^\./, ''))) {
          out.push(child);
        }
        visit(child);
      }
    };
    visit(this);
    return out;
  }

  findButton(label: string): FakeElement | undefined {
    return this.querySelectorAll('button').find((b) => b.textContent === label);
  }

  allText(): string {
    return [this.textContent, ...this.children.filter((c) => !c.hidden).map((c) => c.allText())].filter(Boolean).join(' ');
  }
}

class FakeButtonElement extends FakeElement {
  constructor() {
    super('button');
  }
}

beforeAll(() => {
  if (typeof globalThis.document === 'undefined') {
    Object.assign(globalThis, {
      HTMLButtonElement: FakeButtonElement,
      document: {
        activeElement: null,
        createElement: (tag: string) => (tag === 'button' ? new FakeButtonElement() : new FakeElement(tag)),
      },
      window: {
        matchMedia: () => ({ matches: true }),
      },
    });
  }
});

const stores: EpisodeStore[] = [];
async function openStore(name = crypto.randomUUID()): Promise<EpisodeStore> {
  const store = await EpisodeStore.open(name);
  stores.push(store);
  return store;
}
afterEach(() => {
  for (const store of stores.splice(0)) store.close();
});

function makeInput(activity: string): EpisodeInput {
  return {
    ...blankEpisode(),
    activity,
    outcome: 'completed_with_difficulty',
    duration_minutes: 15,
    recovery_required: true,
    recovery_minutes: 30,
    notes: 'Real production note',
    symptoms: ['fatigue', 'pain'],
  };
}

function makeDescriptor(uid: string, journalId: string): ProductionJournalDescriptor {
  return parseProductionJournalDescriptor(
    {
      owner_uid: uid,
      journal_id: journalId,
      journal_kind: 'production',
      contract_version: 1,
      record_schema_version: 1,
      published_at: Timestamp.fromMillis(1_761_590_000_000),
    },
    journalId,
    uid,
  );
}

function createFakeCloud() {
  const descriptors = new Map<string, Map<string, ProductionJournalDescriptor>>();
  const mailboxes = new Map<
    string,
    {
      changes: Map<string, string>;
      uploadCanonicalChange(payload: string): Promise<'uploaded' | 'already-uploaded'>;
      pullChanges(): Promise<Array<{ canonicalPayload: string }>>;
    }
  >();

  function key(uid: string, journalId: string): string {
    return `${uid}:${journalId}`;
  }

  return {
    publishDescriptor(uid: string, journalId: string) {
      let userMap = descriptors.get(uid);
      if (!userMap) {
        userMap = new Map();
        descriptors.set(uid, userMap);
      }
      const desc = makeDescriptor(uid, journalId);
      userMap.set(journalId, desc);
      return desc;
    },
    mailboxFor(uid: string, journalId: string) {
      const k = key(uid, journalId);
      let mailbox = mailboxes.get(k);
      if (!mailbox) {
        const changes = new Map<string, string>();
        mailbox = {
          changes,
          async uploadCanonicalChange(payload: string) {
            const change = await parseCanonicalChange(payload);
            const existing = changes.get(change.change_id);
            if (existing && existing !== payload) throw new Error('collision');
            changes.set(change.change_id, payload);
            return existing ? 'already-uploaded' : 'uploaded';
          },
          async pullChanges() {
            return [...changes.values()].map((canonicalPayload) => ({ canonicalPayload }));
          },
        };
        mailboxes.set(k, mailbox);
      }
      return mailbox;
    },
    deps(onChange?: () => void) {
      const mailboxFor = this.mailboxFor.bind(this);
      return {
        onChange,
        async discoverJournals(uid: string) {
          const userMap = descriptors.get(uid);
          if (!userMap) return [];
          return [...userMap.values()].sort((a, b) => a.journalId.localeCompare(b.journalId));
        },
        async verifyDescriptor(uid: string, journalId: string) {
          const desc = descriptors.get(uid)?.get(journalId);
          if (!desc) throw new Error('No compatible production journal is available for this Google account.');
          return desc;
        },
        createMailbox(uid: string, journalId: string) {
          return mailboxFor(uid, journalId);
        },
      };
    },
  };
}

describe('PWA production journal discovery, explicit join, isolation, and sync', () => {
  const ownerUid = 'desktop-owner-uid';
  const wrongUid = 'other-google-uid';
  const journalA = '11111111-2222-4333-8444-555555555555';
  const journalB = '66666666-7777-4888-8999-aaaaaaaaaaaa';

  it('validates production journal descriptors and rejects missing or incompatible fields', () => {
    const valid = makeDescriptor(ownerUid, journalA);
    expect(valid.journalId).toBe(journalA);
    expect(valid.journalKind).toBe('production');

    expect(() =>
      parseProductionJournalDescriptor(
        {
          owner_uid: ownerUid,
          journal_id: journalA,
          journal_kind: 'sandbox',
          contract_version: 1,
          record_schema_version: 1,
          published_at: Timestamp.now(),
        },
        journalA,
        ownerUid,
      ),
    ).toThrow('malformed or incompatible');

    expect(() =>
      parseProductionJournalDescriptor(
        {
          owner_uid: ownerUid,
          journal_id: '00000000-0000-0000-0000-000000000000',
          journal_kind: 'production',
          contract_version: 1,
          record_schema_version: 1,
          published_at: Timestamp.now(),
        },
        '00000000-0000-0000-0000-000000000000',
        ownerUid,
      ),
    ).toThrow('invalid journal identity');
  });

  it('reports when no production journal exists and never generates a phone-side journal_id', async () => {
    const cloud = createFakeCloud();
    const prodStore = await openStore();
    const controller = new ProductionSyncController(cloud.deps());
    await controller.attachStore(prodStore);

    await controller.inspectAccount({ uid: ownerUid, email: 'user@example.test' });
    expect(controller.state).toBe('no-journal');
    expect(controller.discovered).toEqual([]);
    expect(controller.selectedJournalId).toBeNull();
    expect(controller.joinedJournalId).toBeNull();
    expect(controller.notice).toContain('No existing production journal was found');
    expect(await prodStore.journalId()).toBeNull();

    // Attempting confirmJoin when no journal exists is a safe no-op
    await controller.confirmJoin();
    expect(await prodStore.journalId()).toBeNull();
  });

  it('requires explicit confirmation to join a single discovered journal, persists journal_id, and survives restart', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);

    // Seed desktop production mailbox with an existing desktop entry + legacy UUIDv5 root
    const desktopStore = await openStore();
    const desktopEntry = await desktopStore.create(makeInput('Desktop morning routine'));
    const desktopMailbox = cloud.mailboxFor(ownerUid, journalA);
    for (const change of await desktopStore.history()) {
      await desktopMailbox.uploadCanonicalChange(canonicalJson(change));
    }

    const prodDbName = crypto.randomUUID();
    const prodStore = await openStore(prodDbName);
    let activatedCount = 0;
    const controller = new ProductionSyncController(cloud.deps());
    await controller.attachStore(prodStore, async () => {
      activatedCount += 1;
    });

    // Authentication + discovery alone must NOT join or pull entries
    await controller.inspectAccount({ uid: ownerUid, email: 'owner@example.test' });
    expect(controller.state).toBe('awaiting-join');
    expect(controller.statusLabel()).toBe('Connected');
    expect(controller.selectedJournalId).toBe(journalA);
    expect(controller.joinedJournalId).toBeNull();
    expect(activatedCount).toBe(0);
    expect(await prodStore.journalId()).toBeNull();
    expect(await prodStore.list()).toEqual([]);

    // Explicit confirmJoin pulls remote graph, materializes entries, persists journal_id, and activates production mode
    await controller.confirmJoin();
    expect(controller.state).toBe('signed-in');
    expect(controller.statusLabel()).toBe('Up to date');
    expect(controller.joinedJournalId).toBe(journalA);
    expect(activatedCount).toBe(1);
    expect(await prodStore.journalId()).toBe(journalA);
    expect((await prodStore.list()).map((r) => r.episode?.activity)).toEqual(['Desktop morning routine']);
    expect((await prodStore.list())[0].record_id).toBe(desktopEntry.record_id);

    // Close and reopen store (simulating PWA reload/restart)
    prodStore.close();
    const reopenedStore = await openStore(prodDbName);
    let reopenedActivated = 0;
    const reopenedController = new ProductionSyncController(cloud.deps());
    await reopenedController.attachStore(reopenedStore, async () => {
      reopenedActivated += 1;
    });
    expect(reopenedActivated).toBe(1);
    expect(reopenedController.joinedJournalId).toBe(journalA);
    expect(await reopenedStore.journalId()).toBe(journalA);
    expect((await reopenedStore.list()).map((r) => r.episode?.activity)).toEqual(['Desktop morning routine']);
  });

  it('requires explicit selection when multiple production journals exist and never picks one arbitrarily', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    cloud.publishDescriptor(ownerUid, journalB);

    const desktopB = await openStore();
    await desktopB.create(makeInput('Entry in Journal B'));
    const mailboxB = cloud.mailboxFor(ownerUid, journalB);
    for (const change of await desktopB.history()) {
      await mailboxB.uploadCanonicalChange(canonicalJson(change));
    }

    const prodStore = await openStore();
    const controller = new ProductionSyncController(cloud.deps());
    await controller.attachStore(prodStore);

    await controller.inspectAccount({ uid: ownerUid, email: 'owner@example.test' });
    expect(controller.state).toBe('awaiting-join');
    expect(controller.discovered.map((d) => d.journalId)).toEqual([journalA, journalB]);
    expect(controller.selectedJournalId).toBeNull();

    // confirmJoin without explicit selection is a no-op
    await controller.confirmJoin();
    expect(await prodStore.journalId()).toBeNull();
    expect(await prodStore.list()).toEqual([]);

    // Explicitly select journalB and confirm join
    controller.selectJournal(journalB);
    expect(controller.selectedJournalId).toBe(journalB);
    await controller.confirmJoin();
    expect(await prodStore.journalId()).toBe(journalB);
    expect((await prodStore.list()).map((r) => r.episode?.activity)).toEqual(['Entry in Journal B']);
  });

  it('keeps disposable sandbox entries isolated and refuses to join into a non-empty unlinked store', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    const desktopStore = await openStore();
    await desktopStore.create(makeInput('Real desktop entry'));
    const mailboxA = cloud.mailboxFor(ownerUid, journalA);
    for (const change of await desktopStore.history()) {
      await mailboxA.uploadCanonicalChange(canonicalJson(change));
    }

    const sandboxStore = await openStore();
    const demoEntry = await sandboxStore.create(makeInput('DEMO: disposable phone entry'));
    const prodStore = await openStore();

    expect(await prodStore.deviceId()).not.toBe(await sandboxStore.deviceId());
    expect(await prodStore.deviceId()).not.toBe(journalA);

    const controller = new ProductionSyncController(cloud.deps());
    await controller.attachStore(prodStore);
    await controller.inspectAccount({ uid: ownerUid });
    await controller.confirmJoin();
    await controller.syncEntries();

    // Sandbox store still has its DEMO entry; production store and mailbox only have the real desktop entry
    expect((await sandboxStore.list()).map((r) => r.record_id)).toEqual([demoEntry.record_id]);
    expect((await prodStore.list()).map((r) => r.episode?.activity)).toEqual(['Real desktop entry']);
    expect(mailboxA.changes.size).toBe(1);

    // If a store unexpectedly contains unlinked local entries before joining, joinProductionJournal fails safely
    const dirtyStore = await openStore();
    await dirtyStore.create(makeInput('Unlinked local entry'));
    await expect(joinProductionJournal(dirtyStore, journalA, mailboxA)).rejects.toThrow(
      'already contains unlinked journal data',
    );
    expect(await dirtyStore.journalId()).toBeNull();
    expect((await dirtyStore.list()).map((r) => r.episode?.activity)).toEqual(['Unlinked local entry']);
  });

  it('protects against signing into the wrong Google account before and after joining', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    const desktopStore = await openStore();
    await desktopStore.create(makeInput('Authoritative desktop entry'));
    const mailboxA = cloud.mailboxFor(ownerUid, journalA);
    for (const change of await desktopStore.history()) {
      await mailboxA.uploadCanonicalChange(canonicalJson(change));
    }

    const prodStore = await openStore();
    const controller = new ProductionSyncController(cloud.deps());
    await controller.attachStore(prodStore);

    // Wrong account before joining sees no journals
    await controller.inspectAccount({ uid: wrongUid, email: 'wrong@example.test' });
    expect(controller.state).toBe('no-journal');
    expect(controller.discovered).toEqual([]);
    expect(await prodStore.journalId()).toBeNull();

    // Sign in with owner account and join
    await controller.inspectAccount({ uid: ownerUid, email: 'owner@example.test' });
    await controller.confirmJoin();
    expect(await prodStore.journalId()).toBe(journalA);

    // Create a local phone entry while disconnected, then sign in with wrong account
    await controller.disconnect();
    await prodStore.create(makeInput('Unsynced phone entry'));
    await controller.inspectAccount({ uid: wrongUid, email: 'wrong@example.test' });
    expect(controller.state).toBe('no-journal');
    expect(controller.notice).toContain('No compatible production journal is available for this Google account');
    await controller.syncEntries();

    // Wrong account mailbox received zero uploads, and local production state is untouched
    expect(cloud.mailboxFor(wrongUid, journalA).changes.size).toBe(0);
    expect(mailboxA.changes.size).toBe(1);
    expect(await prodStore.list()).toHaveLength(2);
  });

  it('synchronizes phone creates, edits, tombstones, conflicts, disconnect/reconnect, and rejects malformed DAGs', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    const desktopStore = await openStore();
    await desktopStore.joinProductionJournal(journalA, [], productionProvider(journalA));
    const shared = await desktopStore.create(makeInput('Shared desktop entry'));
    const mailboxA = cloud.mailboxFor(ownerUid, journalA);
    await syncProductionEntries(desktopStore, journalA, mailboxA);

    // Malformed remote DAG is rejected before mutating an empty joining store
    const badMailbox = {
      async uploadCanonicalChange() { return 'uploaded'; },
      async pullChanges() {
        return [{ canonicalPayload: canonicalJson(fixture.changes[1]) }]; // delete without parent create
      },
    };
    const rejectedStore = await openStore();
    await expect(joinProductionJournal(rejectedStore, journalA, badMailbox)).rejects.toThrow('parent');
    expect(await rejectedStore.journalId()).toBeNull();
    expect(await rejectedStore.history()).toEqual([]);

    // Join phone to journalA
    const phoneStore = await openStore();
    const controller = new ProductionSyncController(cloud.deps());
    await controller.attachStore(phoneStore);
    await controller.inspectAccount({ uid: ownerUid, email: 'owner@example.test' });
    await controller.confirmJoin();
    expect(controller.statusLabel()).toBe('Up to date');
    expect(await phoneStore.list()).toHaveLength(1);

    // Phone creates an entry -> status becomes Connected -> Sync now uploads to shared mailbox -> desktop pulls it
    const phoneCreated = await phoneStore.create(makeInput('Created on phone'));
    controller.localChanged();
    expect(controller.statusLabel()).toBe('Connected');
    await controller.syncEntries();
    expect(controller.statusLabel()).toBe('Up to date');

    await syncProductionEntries(desktopStore, journalA, mailboxA);
    expect((await desktopStore.get(phoneCreated.record_id))?.episode?.activity).toBe('Created on phone');

    // Concurrent edit on desktop and phone -> conflict surfaced on phone -> resolved and synced back to desktop
    const desktopShared = (await desktopStore.get(shared.record_id))!;
    await desktopStore.update({ ...desktopShared.episode!, notes: 'Desktop concurrent edit' }, desktopShared.local_revision);
    await syncProductionEntries(desktopStore, journalA, mailboxA);

    const phoneShared = (await phoneStore.get(shared.record_id))!;
    const phoneEdited = await phoneStore.update({ ...phoneShared.episode!, notes: 'Phone concurrent edit' }, phoneShared.local_revision);
    controller.localChanged();
    await controller.syncEntries();

    expect(controller.statusLabel()).toBe('Needs attention');
    expect(controller.conflicts).toHaveLength(1);
    await controller.resolveConflict(shared.record_id, phoneEdited.local_change_id);
    expect(controller.conflicts).toHaveLength(0);
    await controller.syncEntries();
    expect(controller.statusLabel()).toBe('Up to date');

    await syncProductionEntries(desktopStore, journalA, mailboxA);
    expect((await desktopStore.get(shared.record_id))?.episode?.notes).toBe('Phone concurrent edit');

    // Delete on desktop propagates tombstone to phone
    await desktopStore.delete(phoneCreated.record_id, phoneCreated.local_revision);
    await syncProductionEntries(desktopStore, journalA, mailboxA);
    await controller.syncEntries();
    expect((await phoneStore.get(phoneCreated.record_id))?.deleted).toBe(true);
    expect(await phoneStore.list()).toHaveLength(1);

    // Offline failure preserves local entries and converges on retry
    await phoneStore.create(makeInput('Created while offline'));
    controller.localChanged();
    const pullSpy = vi.spyOn(mailboxA, 'pullChanges').mockRejectedValueOnce(
      Object.assign(new Error('network down'), { code: 'unavailable' }),
    );
    await controller.syncEntries();
    expect(controller.statusLabel()).toBe('Offline');
    expect(await phoneStore.list()).toHaveLength(2);
    pullSpy.mockRestore();

    // Disconnect preserves journal_id and unsynced changes; reconnect syncs normally
    await controller.disconnect();
    expect(controller.statusLabel()).toBe('Not connected');
    expect(await phoneStore.journalId()).toBe(journalA);
    expect(await phoneStore.pending(productionProvider(journalA))).toHaveLength(1);

    await controller.inspectAccount({ uid: ownerUid, email: 'owner@example.test' });
    expect(controller.state).toBe('signed-in');
    expect(controller.statusLabel()).toBe('Connected');
    await controller.syncEntries();
    expect(controller.statusLabel()).toBe('Up to date');
    await syncProductionEntries(desktopStore, journalA, mailboxA);
    expect(await desktopStore.list()).toHaveLength(2);
  });
});

describe('Installed PWA Google authentication lifecycle and panel coordination', () => {
  const ownerUid = 'desktop-owner-uid';
  const journalA = '11111111-2222-4333-8444-555555555555';
  const sandboxJournal = '99999999-8888-4777-8666-555555555555';
  const user: User = { uid: ownerUid, email: 'owner@example.test' } as User;

  function createSharedAuthHarness(initialUser: User | null = null) {
    const listeners = new Set<(u: User | null) => void>();
    const auth = { currentUser: initialUser } as unknown as Auth;
    const runtime: FirebaseRuntime = {
      auth,
      firestore: {} as Firestore,
      emulatorMode: false,
    };
    return {
      auth,
      runtime,
      listeners,
      emitUser(next: User | null) {
        (auth as { currentUser: User | null }).currentUser = next;
        for (const listener of listeners) listener(next);
      },
      observeAuth(_auth: Auth, next: (u: User | null) => void) {
        listeners.add(next);
        queueMicrotask(() => {
          if (listeners.has(next)) next(auth.currentUser);
        });
        return () => {
          listeners.delete(next);
        };
      },
    };
  }

  it('1. Standalone auth success: authenticates once in standalone mode, shows Connected with Google, discovers journal, and performs no auto-join or upload', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    const desktopStore = await openStore();
    await desktopStore.joinProductionJournal(journalA, [], productionProvider(journalA));
    await desktopStore.create(makeInput('Desktop entry awaiting phone join'));
    const mailboxA = cloud.mailboxFor(ownerUid, journalA);
    await syncProductionEntries(desktopStore, journalA, mailboxA);
    const uploadSpy = vi.spyOn(mailboxA, 'uploadCanonicalChange');

    const prodStore = await openStore();
    const harness = createSharedAuthHarness(null);
    let signInCalls = 0;
    let standalonePassed = false;

    const panel = new ProductionSyncPanel({
      ...cloud.deps(),
      initRuntime: async () => harness.runtime,
      observeAuth: harness.observeAuth,
      completeRedirect: async () => null,
      isStandalone: () => true,
      signIn: async (_auth, standalone) => {
        signInCalls += 1;
        standalonePassed = standalone;
        harness.emitUser(user);
        return user;
      },
    });
    await panel.attachStore(prodStore, async () => {});
    await panel.start({ VITE_FIREBASE_PROJECT_ID: 'demo' });

    const root = panel.element as unknown as FakeElement;
    expect(root.allText()).toContain('Not connected');
    expect(root.findButton('Connect with Google')).toBeDefined();

    // User taps Connect with Google once in installed standalone PWA
    await panel.beginSignIn();

    expect(signInCalls).toBe(1);
    expect(standalonePassed).toBe(true);
    expect(root.allText()).toContain('Connected');
    expect(root.allText()).toContain('Connected with Google · owner@example.test');
    expect(root.allText()).toContain('Existing production journal found');
    expect(root.findButton('Join existing journal')?.disabled).toBe(false);

    // Requirement 6: Production authentication alone performs NO automatic join and NO journal upload
    expect(await prodStore.journalId()).toBeNull();
    expect(await prodStore.list()).toEqual([]);
    expect(uploadSpy).not.toHaveBeenCalled();
  });

  it('2. Shared auth lifecycle: SandboxSyncPanel and ProductionSyncPanel share one redirect completion and both observe user state without resetting each other', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    const prodStore = await openStore();
    const sandboxStore = await openStore();
    const harness = createSharedAuthHarness(null);

    // Simulate single-consumer Firebase getRedirectResult behavior: a raw second call would return null
    let rawRedirectCalls = 0;
    const rawGetRedirectResult = vi.fn(async () => {
      rawRedirectCalls += 1;
      if (rawRedirectCalls === 1) {
        (harness.auth as { currentUser: User | null }).currentUser = user;
        return { user };
      }
      return null;
    });

    const sandboxPanel = new SandboxSyncPanel({
      initRuntime: async () => harness.runtime,
      observeAuth: harness.observeAuth,
      completeRedirect: (auth) => completeGoogleRedirect(auth, rawGetRedirectResult),
      getSandboxJournalId: async () => sandboxJournal,
      createTransport: () => ({ journalId: sandboxJournal } as unknown as FirestoreSandboxTransport),
    });
    const productionPanel = new ProductionSyncPanel({
      ...cloud.deps(),
      initRuntime: async () => harness.runtime,
      observeAuth: harness.observeAuth,
      completeRedirect: (auth) => completeGoogleRedirect(auth, rawGetRedirectResult),
    });

    sandboxPanel.attachStore(sandboxStore, async () => {});
    await productionPanel.attachStore(prodStore, async () => {});

    // Start both panels concurrently just like main.ts
    await Promise.all([
      sandboxPanel.start({ VITE_FIREBASE_PROJECT_ID: 'demo' }),
      productionPanel.start({ VITE_FIREBASE_PROJECT_ID: 'demo' }),
    ]);

    // Only ONE redirect completion call occurred, and BOTH panels converged to Connected
    expect(rawGetRedirectResult).toHaveBeenCalledTimes(1);
    const sandboxRoot = sandboxPanel.element as unknown as FakeElement;
    const prodRoot = productionPanel.element as unknown as FakeElement;
    expect(sandboxRoot.allText()).toContain('Connected');
    expect(sandboxRoot.allText()).toContain('Google account: owner@example.test');
    expect(sandboxRoot.findButton('Sync now')?.disabled).toBe(false);
    expect(prodRoot.allText()).toContain('Connected');
    expect(prodRoot.allText()).toContain('Connected with Google · owner@example.test');
    expect(prodRoot.findButton('Join existing journal')?.disabled).toBe(false);
  });

  it('3. Auth ordering: converges to Connected whether onAuthStateChanged fires before redirect completion or redirect completion resolves before observer callback', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    let discoverCountA = 0;
    let discoverCountB = 0;

    // Case 3A: onAuthStateChanged fires null first, then redirect completion resolves with user
    const harnessA = createSharedAuthHarness(null);
    const panelA = new ProductionSyncPanel({
      ...cloud.deps(),
      discoverJournals: async (uid) => {
        discoverCountA += 1;
        return cloud.deps().discoverJournals(uid);
      },
      initRuntime: async () => harnessA.runtime,
      observeAuth: (_auth, next) => {
        // Immediately fire signed-out before redirect completes
        next(null);
        return () => {};
      },
      completeRedirect: async () => {
        await new Promise((r) => setTimeout(r, 5));
        (harnessA.auth as { currentUser: User | null }).currentUser = user;
        return user;
      },
    });
    await panelA.attachStore(await openStore(), async () => {});
    await panelA.start({ VITE_FIREBASE_PROJECT_ID: 'demo' });
    const rootA = panelA.element as unknown as FakeElement;
    expect(rootA.allText()).toContain('Connected');
    expect(rootA.findButton('Join existing journal')?.disabled).toBe(false);
    expect(discoverCountA).toBe(1);

    // Case 3B: redirect completion resolves with user BEFORE auth observer callback fires (even if observer emits stale null then user)
    const harnessB = createSharedAuthHarness(null);
    let lateObserver: ((u: User | null) => void) | undefined;
    const panelB = new ProductionSyncPanel({
      ...cloud.deps(),
      discoverJournals: async (uid) => {
        discoverCountB += 1;
        return cloud.deps().discoverJournals(uid);
      },
      initRuntime: async () => harnessB.runtime,
      observeAuth: (_auth, next) => {
        lateObserver = next;
        return () => {};
      },
      completeRedirect: async () => {
        (harnessB.auth as { currentUser: User | null }).currentUser = user;
        // Stale null arriving while startup redirect is settling must not clobber the redirect user
        lateObserver?.(null);
        return user;
      },
    });
    await panelB.attachStore(await openStore(), async () => {});
    await panelB.start({ VITE_FIREBASE_PROJECT_ID: 'demo' });
    // Now observer also emits the authenticated user; discovery must not be duplicated
    lateObserver?.(user);
    const rootB = panelB.element as unknown as FakeElement;
    expect(rootB.allText()).toContain('Connected');
    expect(rootB.findButton('Join existing journal')?.disabled).toBe(false);
    expect(discoverCountB).toBe(1);
  });

  it('4. Auth failure: surfaces safe visible error without exposing tokens/secrets and without silently falling back to signed-out or false Connected state', async () => {
    const cloud = createFakeCloud();
    const harness = createSharedAuthHarness(null);
    const secretToken = 'ya29.secret-oauth-token-do-not-expose';
    const redirectError = new FirebaseError(
      'auth/unauthorized-domain',
      `https://private.example/callback?access_token=${secretToken}`,
      { accessToken: secretToken },
    );

    const panel = new ProductionSyncPanel({
      ...cloud.deps(),
      initRuntime: async () => harness.runtime,
      // Observer fires null after redirect failure (simulating async initial onAuthStateChanged(null))
      observeAuth: harness.observeAuth,
      completeRedirect: async () => {
        throw redirectError;
      },
    });
    await panel.attachStore(await openStore(), async () => {});
    await panel.start({ VITE_FIREBASE_PROJECT_ID: 'demo' });
    // Wait for async queueMicrotask in observeAuth(null)
    await new Promise((r) => setTimeout(r, 5));

    const root = panel.element as unknown as FakeElement;
    expect(root.allText()).toContain('Needs attention');
    expect(root.allText()).toContain('Could not connect');
    expect(root.allText()).toContain('auth/unauthorized-domain');
    expect(root.allText()).not.toContain(secretToken);
    expect(root.allText()).not.toContain('private.example');
    expect(root.allText()).not.toContain('Connected with Google');
    expect(root.findButton('Join existing journal')).toBeUndefined();
  });

  it('5. Reload persistence: starts Connected when browserLocalPersistence already has an authenticated user even while panel details is closed', async () => {
    const cloud = createFakeCloud();
    cloud.publishDescriptor(ownerUid, journalA);
    const prodStore = await openStore();

    // User is already persisted in browserLocalPersistence on reload; <details> is closed (open = false)
    const harness = createSharedAuthHarness(user);
    const panel = new ProductionSyncPanel({
      ...cloud.deps(),
      initRuntime: async () => harness.runtime,
      observeAuth: harness.observeAuth,
      completeRedirect: async () => null,
    });
    expect((panel.element as unknown as FakeElement).open).toBe(false);
    await panel.attachStore(prodStore, async () => {});
    await panel.start({ VITE_FIREBASE_PROJECT_ID: 'demo' });
    await new Promise((r) => setTimeout(r, 5));

    const root = panel.element as unknown as FakeElement;
    expect(root.allText()).toContain('Connected');
    expect(root.allText()).toContain('Connected with Google · owner@example.test');
    expect(root.findButton('Join existing journal')?.disabled).toBe(false);
  });
});
