# Zymptom PWA M0

A touch-first, installable **disposable mobile proof** in `pwa/`. Use test
entries only. Manual Sandbox Sync can exchange these entries with another PWA
installation. **It does not sync with desktop Zymptom** and cannot open its
SQLite journal. The static preview serves only this build.

## Stack and local commands

Vanilla TypeScript + Vite; native IndexedDB and a small generated service worker.
Firebase's modular Web SDK is used only by the optional Sandbox Sync proof.
Without Firebase configuration, the cloud panel stays unavailable and
normal logging remains local. Vitest + fake-indexeddb cover the model and
storage; Playwright covers the production build in Chrome at mobile sizes.
Dependencies are pinned in `package-lock.json`. Node 22.12+ and npm are required.

```bash
cd /home/natani/Projects/Zymptom/pwa
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Development mode deliberately does **not** register a
service worker. To verify installation/offline use, build and preview instead:

```bash
npm run build
npm run preview
```

Open `http://127.0.0.1:4173`. Expand **About this mobile proof** and wait for
**Offline app shell ready** before going offline. Install through the browser's
install menu if desired. The service worker precaches this exact build's local
HTML, CSS, JS, manifest, and icons. New builds wait for old tabs/windows to close
before replacing the shell; reload after reopening to use the new build. Cached
episode data is never mixed into the service-worker cache.

Vite preview is a development verification server, not a production hosting
service. Any later static deployment must serve `dist/` at its own origin root,
use HTTPS, and serve `sw.js` without a long-lived immutable HTTP cache. Do not
serve the repository root or a desktop data directory.

## Test on Android

1. Connect your Android phone to the computer with a USB data cable.
2. On the phone, enable Developer Options and **USB debugging** if needed. Unlock
   it and accept the **Allow USB debugging?** prompt.
3. In this directory run:

   ```bash
   npm run phone:android
   ```

4. Open **http://localhost:4173/** in Chrome on the phone.
5. Wait for **Offline app shell ready**, then use Chrome's menu to install Zymptom
   if desired. Keep the terminal open while testing; press **Ctrl+C** when finished.

The helper builds the PWA, creates an `adb reverse tcp:4173 tcp:4173` USB tunnel,
and starts a loopback-only production preview. The phone reaches the computer's
preview as its own `localhost`, so no shared Wi-Fi or certificate setup is needed.
USB tethering is not needed; USB debugging is what lets ADB connect. On normal
exit or Ctrl+C, the helper stops the preview and removes its ADB forward. If more
than one device is attached, select one with
`ANDROID_SERIAL=<device-id> npm run phone:android` (`adb devices -l` lists IDs).

### Android troubleshooting

- **`adb` is missing:** Install Android Platform Tools. On Linux Mint/LMDE run
  `sudo apt install adb`, then rerun `npm run phone:android`. The helper never
  installs system packages.
- **No phone is listed:** Reconnect the USB data cable, enable Developer Options
  and USB debugging, unlock the phone, accept its authorization prompt, then rerun.
- **Phone says unauthorized:** Unlock it and accept the **Allow USB debugging?**
  prompt. If no prompt appears, disconnect/reconnect USB and check the phone's
  USB debugging setting.
- **Port 4173 is busy:** Stop the other preview with Ctrl+C in its terminal and
  rerun. If a previous run was forcibly killed and left an ADB mapping, remove it
  with `adb -s <device-id> reverse --remove tcp:4173` and rerun.
- **Dependencies are missing:** Run `npm ci` from `pwa/`, then rerun the helper.

After installing, log a `DEMO:` episode, close/reopen the app, edit it, and try a
confirmed deletion. Once the shell reports ready, disconnect Wi-Fi/mobile data
and confirm local CRUD still works. Activity and Notes are ordinary text fields;
the phone keyboard's own microphone can enter text there. Dictation availability
and any offline processing depend on the keyboard. Zymptom adds no microphone
capture controls.

## iPhone or advanced LAN testing

iPhone testing remains a manual LAN/HTTPS path. The same alternative also works
for Android when USB debugging is unavailable. Phone installation and service
workers need a trusted HTTPS origin; a plain LAN HTTP address or a certificate
warning does not provide that.

Install [mkcert](https://github.com/FiloSottile/mkcert#mobile-devices), replace
the example IP with this computer's LAN address, and run:

```bash
cd /home/natani/Projects/Zymptom/pwa
PWA_LAN_IP=192.168.1.50
mkdir -p .local-tls
mkcert -install
mkcert -cert-file .local-tls/pwa.pem -key-file .local-tls/pwa-key.pem localhost 127.0.0.1 "$PWA_LAN_IP"
npm run build
PWA_TLS_CERT="$PWD/.local-tls/pwa.pem" PWA_TLS_KEY="$PWD/.local-tls/pwa-key.pem" npm run preview -- --host "$PWA_LAN_IP"
```

Follow mkcert's mobile instructions to trust **only its public `rootCA.pem`** on
the test phone. Never copy/share `rootCA-key.pem` or `pwa-key.pem`. Keys and
certificates under `.local-tls/` are git-ignored. Open
`https://<your-LAN-IP>:4173/` on the phone without bypassing a certificate
warning. On iPhone, use Safari's Share → Add to Home Screen option, then launch
from the new icon. Use the same origin throughout testing: different origins
have separate browser-local data.

## Storage and safety boundary

IndexedDB database **`zymptom-pwa-m0-disposable`**, version 2, is scoped to the
browser origin/profile. `episodes` holds local record heads and tombstones;
`changes`, `conflicts`, and `acknowledgements` hold immutable history and sync
state; `installation` holds one persistent random device UUID. `EpisodeStore` owns
transactions and exposes async create/update/delete/get/list methods. UI state
is separate. A successful save waits for transaction completion, stale cross-tab
writes are rejected, and failed writes retain the editor draft. Deletion requires
a dialog identifying the saved activity and local time; Cancel is non-destructive.

UUIDs, complete FunctionalEpisode fields, UTC timestamps (six fractional digits),
Unicode/multiline text, ordered lists, null, zero, false, and empty note strings
are preserved. Unchanged occurred times retain their full precision and DST fold;
new times use the browser's local zone, rejecting nonexistent local wall times.
Optional recovery information is not inferred from duration. Multi-select Add
uses selection order, skips duplicates, and resets checks; successful storage
writes also normalize lists by first occurrence without mutating the caller.

This is **browser storage, not desktop SQLite or a backup**. It persists across
normal relaunches, but clearing site data, private browsing, browser removal,
storage eviction, or device loss can remove it. There are no M0 exports/backups.
No seed data is inserted automatically. Today shows the browser's local calendar
day; there is no History screen in M0. An entry moved to another day remains
stored but is outside Today.

To reset development test data, open the dev-server console and run:

```js
await resetZymptomProof()
```

It requires confirmation, clears the disposable local entries and sync history, and preserves
the installation identity. It does not delete cloud mailbox history, which can
reappear on a later sync. This helper is absent from production builds. For a
complete production-preview reset, use that test origin's browser **Clear site
data** control; it removes both its IndexedDB and offline shell. Never use it on
an unrelated origin. No reset can touch desktop SQLite or another browser origin.

## Z5.0 contract compatibility

See [sync contract v1](../docs/sync-contract-v1.md). This proof mirrors the exact
FunctionalEpisode snapshot fields and current persisted enum/tag identifiers,
using desktop-generated labels/colors. `tests/fixtures/z5-v1.json` is synthetic
canonical JSON produced by Python's actual `Change`/`SyncBatch` implementation,
including an active snapshot and its tombstone. Tests validate its episode shape,
values, order, timestamp precision, Unicode, and null/zero/false distinctions.
The additional `src/sandbox/synthetic-v1.json` is generated from the same Python
fixture builder for the sandbox-only transport panel.

Generate or check the references without opening a journal or device config:

```bash
npm run check:desktop
# Intentional refresh, after a reviewed desktop contract change:
../.venv/bin/python scripts/desktop-fixtures.py
../.venv/bin/python scripts/branding-icons.py
```

Local heads are materialized wrappers; only canonical immutable Changes are
uploaded. On first opening an M0 database, each active entry becomes a single
bootstrap create root using its existing record and local change IDs. No older
ancestry is invented. M0 tombstones have no surviving snapshot or ancestry, so
they remain reserved local tombstones and are not uploaded; an incoming record
with the same ID is rejected rather than resurrected. Bootstrap is transactional
and idempotent. This disposable browser proof does not provide desktop's backup
before merge; browser storage is not a backup.

## Z5.2 Firebase Sandbox Sync

**Testing only:** create disposable `DEMO:` entries, sign in to the same sandbox
account on each PWA, and press **Sync disposable entries** on each device. Sync
uploads canonical changes, pulls and validates the mailbox, then merges locally.
Repeat after edits or deletes. Local logging works signed out or offline. A sync
failure leaves local work available and can be retried. Firestore is an immutable
mailbox; it does not decide conflicts. The PySide/SQLite journal is not connected.

Concurrent edits, including delete versus edit, remain explicit conflicts; neither
branch is silently chosen. The developer proof panel shows current candidates
and lets you inspect and choose one. Resolution creates a revision with all
current tips as parents; sync again to share it. Ordinary edits/deletes of a
conflicted entry are blocked until resolution. This is not a production resolver.

### Configure a sandbox Firebase project

1. Create a dedicated Firebase sandbox project and register a Web app.
2. In Authentication, enable the Google provider. **For projects created after
   April 28, 2025, add `localhost` under Authentication → Settings → Authorized
   domains before testing the Android `http://localhost:4173` workflow.**
3. Create Cloud Firestore in production/locked mode.
4. Copy `.env.example` to `.env.local` and fill in the Web app's `apiKey`,
   `authDomain`, `projectId`, and `appId`. `storageBucket` and
   `messagingSenderId` are optional. These are public Web configuration values;
   never put service-account files, private keys, OAuth client secrets, or user
   tokens in this PWA.
5. From `pwa/`, install the checked-in dependencies and deploy the checked-in
   deny-by-default Rules:

   ```bash
   npm ci
   npm exec -- firebase login
   npm run firebase:deploy-rules -- --project YOUR_SANDBOX_PROJECT_ID
   ```

6. Run `npm run build` and `npm run preview`, or use `npm run phone:android` for
   Android. Expand **Sandbox Sync**, sign in with Google, and press **Sync
   disposable entries**. Signing out leaves browser-local entries intact.

The Firebase config is read from ignored `pwa/.env.local`; no project-specific
config is committed. Google sign-in uses a popup in a normal browser and a
redirect from an installed standalone PWA. Firebase Auth restores its local
session when supported. Firestore offline persistence is not enabled. Local CRUD
does not wait for or depend on Auth or Firestore.

The sandbox journal ID is a random opaque UUID stored at
`users/{uid}/sandbox/identity`; the create-only identity transaction makes all
installations for one Firebase UID discover the same journal. Change documents
are stored under `users/{uid}/journals/{journalId}/changes/{changeId}`. Their
`canonical_payload_json` string is authoritative; version, IDs, hash, owner, and
server upload time are separate transport metadata. Pull validates canonical
JSON, the v1 snapshot, and its SHA-256 hash before exposing the packet. An exact
repeat is idempotent; a same-ID different payload is rejected. Clients cannot
update or delete history. Reconciliation happens in the PWA, never in Firestore.

### Emulator verification

The repository includes Firebase Auth and Firestore emulator configuration and
Rules tests. No live Firebase project or credentials are needed:

```bash
npm run firebase:test
```

This starts Auth on port 9099 and Firestore on port 8081, runs Rules and
transport tests, builds an emulator-configured PWA, and tests the Google popup
flow in Chrome. To start the emulators for interactive development, run `npm run firebase:emulators` in a
separate terminal. `npm run firebase:check` runs emulator checks, a production
build, and desktop fixture parity. Emulator tests use the `demo-zymptom-z51`
project ID and cannot write to a live project.

### Two-device manual proof

With the same sandbox `.env.local` on both installations:

1. Sign Device A and Device B into the same Google account.
2. Create different `DEMO:` entries on A and B. Sync A, then B, then A; both
   should show both entries. Edit one on A, sync A and B, and check B's edit.
3. Delete a shared entry on B, sync B and A, and check it disappears on A.
4. On another shared entry, edit differently on both without syncing. Sync A,
   then B, then A; inspect both candidates and explicitly choose one. Sync the
   resolution on both devices and confirm convergence.

No production Firebase project or physical Android session is configured by
these emulator tests; perform the manual proof only with the dedicated sandbox
project and disposable test data.

## Verification commands

```bash
npm test
npm run build
npm run check:desktop
npm run test:e2e
npm run firebase:test
git diff --check
```

Browser tests use installed Google Chrome (`channel: chrome`), isolated temporary
profiles, synthetic entries, and a loopback-only production preview. OS integration
directories for the installation test are isolated under its temporary directory.
They check manifest/installability, actual Chrome installation/standalone launch,
the offline app shell and CRUD, full browser restart,
confirmed/cancelled deletion, stale-write safety, all five multi-select controls,
and overflow at 320/360/390/430 px. They do not open the desktop journal. Native Qt
audio and desktop regressions are not needed because no desktop code is changed.
