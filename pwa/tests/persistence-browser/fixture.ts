import { createUserWithEmailAndPassword } from 'firebase/auth';
import { collection, doc, getDocsFromServer, serverTimestamp, setDoc } from 'firebase/firestore';
import { initializeFirebaseRuntime } from '../../src/sandbox/firebase';
import { EpisodeStore, PRODUCTION_DATABASE_NAME } from '../../src/storage';

const journal = '11111111-2222-4333-8444-555555555555';

export async function prepareSyntheticJournal(): Promise<void> {
  const runtime = await initializeFirebaseRuntime(import.meta.env);
  if (!runtime?.emulatorMode || runtime.auth.app.options.projectId !== 'demo-zymptom-z51') {
    throw new Error('This fixture requires the disposable Firebase emulators.');
  }
  const account = await createUserWithEmailAndPassword(
    runtime.auth, `synthetic-${crypto.randomUUID()}@example.test`, 'synthetic-password-123',
  );
  await setDoc(doc(runtime.firestore, 'users', account.user.uid, 'journals', journal), {
    owner_uid: account.user.uid, journal_id: journal, journal_kind: 'production',
    contract_version: 1, record_schema_version: 1, published_at: serverTimestamp(),
  });
}

export async function cloudChangeCount(): Promise<number> {
  const runtime = await initializeFirebaseRuntime(import.meta.env);
  if (!runtime?.emulatorMode || !runtime.auth.currentUser || runtime.auth.app.options.projectId !== 'demo-zymptom-z51') {
    throw new Error('This fixture requires a signed-in disposable emulator account.');
  }
  return (await getDocsFromServer(collection(runtime.firestore, 'users', runtime.auth.currentUser.uid, 'journals', journal, 'changes'))).size;
}

export async function joinedJournal(): Promise<string | null> {
  const store = await EpisodeStore.open(PRODUCTION_DATABASE_NAME);
  try { return await store.journalId(); } finally { store.close(); }
}
