import { describe, expect, it, vi } from 'vitest';
import { FirebaseError } from 'firebase/app';
import type { Auth, User } from 'firebase/auth';
import { completeGoogleRedirect, firebaseConfigFromEnv, firebaseErrorMessage, safeFirebaseAuthErrorShape,
  formatSafeFirebaseAuthErrorShape, useRedirectForGoogleSignIn } from '../../src/sandbox/firebase';
import { sandboxJournalPath } from '../../src/sandbox/transport';

describe('Firebase sandbox configuration and UI state helpers', () => {
  it('treats absent Firebase configuration as an optional unavailable transport', () => {
    expect(firebaseConfigFromEnv({})).toBeUndefined();
  });

  it('accepts only the public web-app fields required to initialize Firebase', () => {
    expect(firebaseConfigFromEnv({
      VITE_FIREBASE_API_KEY: 'public-api-key',
      VITE_FIREBASE_AUTH_DOMAIN: 'demo.firebaseapp.com',
      VITE_FIREBASE_PROJECT_ID: 'demo-project',
      VITE_FIREBASE_APP_ID: '1:2:web:3',
      VITE_FIREBASE_STORAGE_BUCKET: 'demo.appspot.com',
    })).toEqual({
      apiKey: 'public-api-key', authDomain: 'demo.firebaseapp.com', projectId: 'demo-project',
      appId: '1:2:web:3', storageBucket: 'demo.appspot.com',
    });
    expect(() => firebaseConfigFromEnv({ VITE_FIREBASE_PROJECT_ID: 'half-configured' })).toThrow('configuration is incomplete');
  });

  it('uses popup auth in both browser and standalone PWA modes and deduplicates redirect completion per Auth instance', async () => {
    expect(useRedirectForGoogleSignIn(false)).toBe(false);
    expect(useRedirectForGoogleSignIn(true)).toBe(false);

    const fakeUser = { uid: 'redirect-user-1', email: 'user@example.test' } as User;
    const fakeAuth = { currentUser: null } as unknown as Auth;
    const getResult = vi.fn(async () => ({ user: fakeUser }));
    const [first, second] = await Promise.all([
      completeGoogleRedirect(fakeAuth, getResult),
      completeGoogleRedirect(fakeAuth, getResult),
    ]);
    expect(getResult).toHaveBeenCalledTimes(1);
    expect(first).toBe(fakeUser);
    expect(second).toBe(fakeUser);
  });

  it('constructs user-owned sandbox paths from Firebase UID and opaque journal ID', () => {
    expect(sandboxJournalPath('firebase-uid-123', '00000000-0000-0000-0000-000000000001'))
      .toBe('users/firebase-uid-123/journals/00000000-0000-0000-0000-000000000001');
    expect(() => sandboxJournalPath('uid/other', '00000000-0000-0000-0000-000000000001')).toThrow();
  });

  it('turns auth, permission, network, and unsupported-contract errors into clear messages', () => {
    expect(firebaseErrorMessage({ code: 'auth/unauthorized-domain' })).toContain('Add localhost');
    expect(firebaseErrorMessage({ code: 'permission-denied' })).toContain('rules');
    expect(firebaseErrorMessage({ code: 'unavailable' })).toContain('Local entries remain available');
    const unsupported = Object.assign(new Error('Unsupported Z5 contract version.'), { name: 'Z5PacketError' });
    expect(firebaseErrorMessage(unsupported)).toBe('Unsupported Z5 contract version.');
  });

  it('shows known and unknown Firebase Auth codes without exposing the raw error message', () => {
    const unauthorized = firebaseErrorMessage({ code: 'auth/unauthorized-domain' });
    expect(unauthorized).toContain('auth/unauthorized-domain');
    expect(unauthorized).toContain('Add localhost');
    const popupBlocked = firebaseErrorMessage({ code: 'auth/popup-blocked' });
    expect(popupBlocked).toContain('Google sign-in failed: auth/popup-blocked');
    expect(popupBlocked).toContain('Allow popups');

    const privateDetails = 'access_token=sample id_token=sample refresh_token=sample';
    const unknownAuthError = Object.assign(new Error(`Firebase: ${privateDetails}`), {
      code: 'auth/credential-already-in-use',
    });
    const unknown = firebaseErrorMessage(unknownAuthError);
    expect(unknown).toContain('auth/credential-already-in-use');
    expect(unknown).toContain('Google sign-in failed');
    expect(unknown).not.toContain(privateDetails);
    expect(unknown).not.toContain('access_token');
  });

  it('reports only the safe shape of FirebaseError-like values', () => {
    class FirebaseErrorLike extends Error {
      code = 'auth/internal-error';
      customData = { accessToken: 'do-not-display' };
      constructor() {
        super('refresh_token=do-not-display');
        this.name = 'FirebaseError';
      }
    }
    const failure = new FirebaseErrorLike();
    const shape = safeFirebaseAuthErrorShape(failure);
    expect(shape).toMatchObject({ type: 'FirebaseErrorLike', name: 'FirebaseError', code: 'auth/internal-error', isError: true });
    expect(shape.ownEnumerablePropertyNames).toEqual(expect.arrayContaining(['code', 'customData', 'name']));
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('do-not-display');
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('refresh_token');
  });

  it('shows a real FirebaseError auth code while keeping its message and custom data private', () => {
    const privateMessage = 'https://sensitive.example/path?access_token=private-value';
    const failure = new FirebaseError('auth/unauthorized-domain', privateMessage, {
      accessToken: 'private-access-token', refreshToken: 'private-refresh-token',
    });
    const shape = safeFirebaseAuthErrorShape(failure);
    const details = formatSafeFirebaseAuthErrorShape(failure);
    expect(shape).toMatchObject({
      type: 'FirebaseError', name: 'FirebaseError', code: 'auth/unauthorized-domain', isError: true,
    });
    expect(shape.ownEnumerablePropertyNames).toEqual(expect.arrayContaining(['code', 'customData', 'name']));
    expect(details).toContain('code=auth/unauthorized-domain');
    expect(details).not.toContain('sensitive.example');
    expect(details).not.toContain('private-value');
    expect(details).not.toContain('private-access-token');
    expect(details).not.toContain('private-refresh-token');
    for (const code of ['auth/unauthorized-domain', 'auth/popup-blocked', 'auth/network-request-failed']) {
      expect(safeFirebaseAuthErrorShape(new FirebaseError(code, privateMessage)).code).toBe(code);
    }
    const segmentedCode = 'auth/provider/reauth-required_2';
    expect(safeFirebaseAuthErrorShape(new FirebaseError(segmentedCode, privateMessage)).code).toBe(segmentedCode);

    const unsafeCode = new FirebaseError('https://sensitive.example/token', 'message');
    expect(safeFirebaseAuthErrorShape(unsafeCode).code).toBe('[redacted]');
  });

  it('allows the exact SDK-derived API-key error while rejecting arbitrary dotted codes', () => {
    const code = 'auth/api-key-not-valid.-please-pass-a-valid-api-key.';
    const failure = new FirebaseError(code, 'https://private.example/?token=never-display', {
      accessToken: 'never-display',
    });
    expect(safeFirebaseAuthErrorShape(failure).code).toBe(code);
    expect(firebaseErrorMessage(failure)).toContain(code);
    expect(firebaseErrorMessage(failure)).toContain('Firebase rejected the sandbox Web API key');
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('private.example');
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('never-display');
    expect(safeFirebaseAuthErrorShape(new FirebaseError('auth/private.example', 'message')).code).toBe('[redacted]');
  });

  it('reports an Error without a code without including its message', () => {
    const failure = new Error('sensitive message content');
    expect(safeFirebaseAuthErrorShape(failure)).toMatchObject({
      type: 'Error', name: 'Error', code: null, isError: true, ownEnumerablePropertyNames: [],
    });
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('sensitive message content');
  });

  it('reports code and property names for a plain object without displaying property values', () => {
    const failure = { name: 'FirebaseError', code: 'auth/unauthorized-domain', accessToken: 'secret-value' };
    const shape = safeFirebaseAuthErrorShape(failure);
    expect(shape).toMatchObject({ type: 'Object', name: 'FirebaseError', code: 'auth/unauthorized-domain', isError: false });
    expect(shape.ownEnumerablePropertyNames).toEqual(expect.arrayContaining(['name', 'code', 'accessToken']));
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('secret-value');
  });

  it('describes unknown thrown values without exposing their descriptions', () => {
    const failure = Symbol('private thrown value');
    expect(safeFirebaseAuthErrorShape(failure)).toMatchObject({
      type: 'Symbol', name: null, code: null, isError: false, ownEnumerablePropertyNames: [],
    });
    expect(formatSafeFirebaseAuthErrorShape(failure)).not.toContain('private thrown value');
  });
});
