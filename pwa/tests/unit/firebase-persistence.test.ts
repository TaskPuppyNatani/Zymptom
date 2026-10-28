import { expect, it, vi } from 'vitest';

const sdk = vi.hoisted(() => {
  let finishReady!: () => void;
  const ready = new Promise<void>((resolve) => { finishReady = resolve; });
  const user = { uid: 'test-user' };
  const auth = { currentUser: user, authStateReady: vi.fn(() => ready) };
  return {
    auth, user, finishReady, persistence: { type: 'LOCAL' },
    setPersistence: vi.fn(async () => {}), signOut: vi.fn(async () => {}),
  };
});

vi.mock('firebase/app', () => ({
  getApps: () => [], initializeApp: (options: unknown) => ({ options }),
}));
vi.mock('firebase/auth', () => ({
  browserLocalPersistence: sdk.persistence, getAuth: () => sdk.auth,
  setPersistence: sdk.setPersistence, signOut: sdk.signOut,
}));
vi.mock('firebase/firestore', () => ({ getFirestore: () => ({}) }));

import { initializeFirebaseRuntime } from '../../src/sandbox/firebase';

it('waits for Firebase local persistence and the initial auth state without signing out', async () => {
  let settled = false;
  const runtime = initializeFirebaseRuntime({
    VITE_FIREBASE_API_KEY: 'test-key', VITE_FIREBASE_AUTH_DOMAIN: 'localhost',
    VITE_FIREBASE_PROJECT_ID: 'test-project', VITE_FIREBASE_APP_ID: 'test-app',
  }).then((value) => { settled = true; return value; });
  await Promise.resolve();
  await Promise.resolve();
  expect(sdk.setPersistence).toHaveBeenCalledWith(sdk.auth, sdk.persistence);
  expect(sdk.auth.authStateReady).toHaveBeenCalledOnce();
  expect(settled).toBe(false);
  expect(sdk.signOut).not.toHaveBeenCalled();
  sdk.finishReady();
  expect((await runtime)?.auth.currentUser).toBe(sdk.user);
  expect(sdk.signOut).not.toHaveBeenCalled();
});
