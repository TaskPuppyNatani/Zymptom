import { getApps, initializeApp, type FirebaseOptions } from 'firebase/app';
import {
  browserLocalPersistence,
  connectAuthEmulator,
  getAuth,
  getRedirectResult,
  GoogleAuthProvider,
  onAuthStateChanged,
  setPersistence,
  signInWithPopup,
  signInWithRedirect,
  signOut,
  type Auth,
  type User,
} from 'firebase/auth';
import { connectFirestoreEmulator, getFirestore, type Firestore } from 'firebase/firestore';

const APP_NAME = 'zymptom-z51-sandbox';

export interface FirebaseWebConfig extends FirebaseOptions {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

export type FirebaseEnvironment = Record<string, unknown>;

export interface FirebaseRuntime {
  auth: Auth;
  firestore: Firestore;
  emulatorMode: boolean;
}

export function firebaseConfigFromEnv(env: FirebaseEnvironment): FirebaseWebConfig | undefined {
  const keys = ['VITE_FIREBASE_API_KEY', 'VITE_FIREBASE_AUTH_DOMAIN', 'VITE_FIREBASE_PROJECT_ID', 'VITE_FIREBASE_APP_ID'] as const;
  const values = keys.map((key) => typeof env[key] === 'string' ? (env[key] as string).trim() : '');
  if (values.every((value) => value === '')) return undefined;
  if (values.some((value) => value === '')) {
    throw new Error('Firebase Sandbox Sync configuration is incomplete. Fill the required values in pwa/.env.local.');
  }
  const [apiKey, authDomain, projectId, appId] = values;
  const optional = (key: string) => typeof env[key] === 'string' && (env[key] as string).trim() !== '' ? (env[key] as string).trim() : undefined;
  return {
    apiKey, authDomain, projectId, appId,
    ...(optional('VITE_FIREBASE_STORAGE_BUCKET') ? { storageBucket: optional('VITE_FIREBASE_STORAGE_BUCKET') } : {}),
    ...(optional('VITE_FIREBASE_MESSAGING_SENDER_ID') ? { messagingSenderId: optional('VITE_FIREBASE_MESSAGING_SENDER_ID') } : {}),
  };
}

export function useRedirectForGoogleSignIn(standalone: boolean): boolean { return standalone; }

// Firebase's backend-error fallback preserves punctuation when it derives this
// fixed identifier. Allowlist the exact code, never arbitrary dotted text/URLs.
const INVALID_API_KEY_SERVER_CODE = 'auth/api-key-not-valid.-please-pass-a-valid-api-key.';

function isSafeFirebaseErrorCode(code: unknown): code is string {
  return typeof code === 'string' && code.length <= 80
    && (/^[a-z][a-z0-9_-]*(?:\/[a-z0-9_-]+)*$/.test(code) || code === INVALID_API_KEY_SERVER_CODE);
}

function safeFirebaseErrorCode(error: unknown): string | undefined {
  try {
    if ((typeof error !== 'object' && typeof error !== 'function') || error === null || !('code' in error)) return undefined;
    const code = Reflect.get(error, 'code') as unknown;
    return isSafeFirebaseErrorCode(code) ? code : undefined;
  } catch {
    return undefined;
  }
}

export interface SafeFirebaseAuthErrorShape {
  type: string;
  name: string | null;
  code: string | null;
  isError: boolean;
  ownEnumerablePropertyNames: string[];
}

function safePropertyString(value: unknown, property: string, pattern: RegExp): string | null {
  try {
    if ((typeof value !== 'object' && typeof value !== 'function') || value === null || !(property in value)) return null;
    const candidate = Reflect.get(value, property) as unknown;
    if (candidate === undefined || candidate === null) return null;
    if (typeof candidate !== 'string' || candidate.length > 80 || !pattern.test(candidate)) return '[redacted]';
    return candidate;
  } catch {
    return '[unavailable]';
  }
}

function safeFirebaseAuthCode(value: unknown): string | null {
  try {
    if ((typeof value !== 'object' && typeof value !== 'function') || value === null || !('code' in value)) return null;
    const code = Reflect.get(value, 'code') as unknown;
    return isSafeFirebaseErrorCode(code) && code.startsWith('auth/')
      ? code
      : '[redacted]';
  } catch {
    return '[unavailable]';
  }
}

function safeConstructorType(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'undefined') return 'undefined';
  if (typeof value !== 'object' && typeof value !== 'function') {
    switch (typeof value) {
      case 'string': return 'String';
      case 'number': return 'Number';
      case 'boolean': return 'Boolean';
      case 'bigint': return 'BigInt';
      case 'symbol': return 'Symbol';
      default: return 'unknown';
    }
  }
  try {
    const prototype = Object.getPrototypeOf(value) as { constructor?: unknown } | null;
    const constructor = prototype?.constructor;
    if (typeof constructor === 'function') {
      const name = Reflect.get(constructor, 'name') as unknown;
      if (typeof name === 'string' && /^[A-Za-z_$][A-Za-z0-9_$]{0,79}$/.test(name)) return name;
    }
  } catch { /* A thrown proxy or custom prototype is reported as Object. */ }
  return 'Object';
}

function safeOwnEnumerablePropertyNames(value: unknown): string[] {
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return [];
  try {
    const keys = Object.keys(value);
    const names = keys.slice(0, 30).map((key) => /^[A-Za-z_$][A-Za-z0-9_$]{0,79}$/.test(key) ? key : '[redacted]');
    if (keys.length > names.length) names.push('[truncated]');
    return names;
  } catch {
    return ['[unavailable]'];
  }
}

export function safeFirebaseAuthErrorShape(value: unknown): SafeFirebaseAuthErrorShape {
  let isError = false;
  try { isError = value instanceof Error; } catch { /* Keep inspecting the remaining safe shape. */ }
  return {
    type: safeConstructorType(value),
    name: safePropertyString(value, 'name', /^[A-Za-z_$][A-Za-z0-9_$.-]*$/),
    code: safeFirebaseAuthCode(value),
    isError,
    ownEnumerablePropertyNames: safeOwnEnumerablePropertyNames(value),
  };
}

export function formatSafeFirebaseAuthErrorShape(value: unknown): string {
  const shape = safeFirebaseAuthErrorShape(value);
  const propertyNames = shape.ownEnumerablePropertyNames.length
    ? shape.ownEnumerablePropertyNames.join(', ')
    : '(none)';
  return `Auth error shape: type=${shape.type}; name=${shape.name ?? '(absent)'}; code=${shape.code ?? '(absent)'}; isError=${shape.isError}; own enumerable properties=[${propertyNames}]`;
}

export function firebaseErrorMessage(error: unknown): string {
  const code = safeFirebaseErrorCode(error);
  if (error instanceof Error && error.name === 'Z5PacketError') return error.message;
  if (error instanceof Error && error.name === 'SandboxIdentityCollisionError') return error.message;

  const knownMessage: Record<string, string> = {
    [INVALID_API_KEY_SERVER_CODE]: 'Firebase rejected the sandbox Web API key. Check the configured Web API key and its API restrictions.',
    'auth/invalid-api-key': 'Firebase rejected the sandbox Web API key. Check the configured Web API key and its API restrictions.',
    'auth/unauthorized-domain': 'This origin is not authorized for Firebase Authentication. Add localhost in Firebase Authentication → Settings → Authorized domains.',
    'auth/operation-not-allowed': 'Google sign-in is disabled. Enable the Google provider in Firebase Authentication.',
    'auth/popup-closed-by-user': 'The Google sign-in window was closed before sign-in finished.',
    'auth/popup-blocked': 'The sign-in window was blocked. Allow popups or open the installed app and try again.',
    'auth/user-token-expired': 'Your sandbox sign-in expired. Sign in again to continue.',
    'firestore/unauthenticated': 'Your sandbox sign-in expired. Sign in again to continue.',
    unauthenticated: 'Your sandbox sign-in expired. Sign in again to continue.',
    'firestore/permission-denied': 'Firebase denied this sandbox request. Check the deployed Firestore rules and signed-in account.',
    'permission-denied': 'Firebase denied this sandbox request. Check the deployed Firestore rules and signed-in account.',
    'firestore/unavailable': 'Firebase Sandbox Sync is offline or unavailable. Local entries remain available.',
    unavailable: 'Firebase Sandbox Sync is offline or unavailable. Local entries remain available.',
    'auth/network-request-failed': 'Firebase Sandbox Sync is offline or unavailable. Local entries remain available.',
  };

  if (code?.startsWith('auth/')) {
    if (code === 'auth/popup-closed-by-user') return `Google sign-in canceled: ${code}.`;
    return `Google sign-in failed: ${code}. ${knownMessage[code] ?? 'Firebase Authentication returned an unrecognized error.'}`;
  }
  if (code && knownMessage[code]) return `${knownMessage[code]} (${code})`;
  if (code) return `Sandbox Sync failed: ${code}. Local entries remain available; check the Firebase setup and retry.`;

  return 'Sandbox Sync could not complete. Local entries remain available; check the Firebase setup and retry.';
}

export async function initializeFirebaseRuntime(env: FirebaseEnvironment): Promise<FirebaseRuntime | undefined> {
  const config = firebaseConfigFromEnv(env);
  if (!config) return undefined;
  const emulatorMode = env.VITE_FIREBASE_USE_EMULATORS === 'true';
  const app = getApps().find((candidate) => candidate.name === APP_NAME)
    ?? initializeApp(config, APP_NAME);
  if (app.options.projectId !== config.projectId || app.options.appId !== config.appId) {
    throw new Error('Firebase Sandbox Sync was already initialized with a different project configuration.');
  }
  const auth = getAuth(app);
  const firestore = getFirestore(app);
  if (emulatorMode) {
    connectAuthEmulator(auth, 'http://127.0.0.1:9099', { disableWarnings: true });
    connectFirestoreEmulator(firestore, '127.0.0.1', 8081);
  }
  await setPersistence(auth, browserLocalPersistence);
  return { auth, firestore, emulatorMode };
}

export function observeSandboxUser(auth: Auth, next: (user: User | null) => void, failure: (error: Error) => void): () => void {
  return onAuthStateChanged(auth, next, failure);
}

export async function completeGoogleRedirect(auth: Auth): Promise<void> {
  await getRedirectResult(auth);
}

export async function signInWithGoogle(auth: Auth, standalone = false): Promise<void> {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: 'select_account' });
  if (useRedirectForGoogleSignIn(standalone)) await signInWithRedirect(auth, provider);
  else await signInWithPopup(auth, provider);
}

export async function signOutSandbox(auth: Auth): Promise<void> { await signOut(auth); }
