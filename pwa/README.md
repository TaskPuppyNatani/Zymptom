# Zymptom PWA M0

A touch-first, installable **disposable mobile proof** in `pwa/`. Use synthetic
test entries only. **It does not sync with desktop Zymptom.** It cannot open the
desktop SQLite journal and has no journal import, backend, transport, accounts,
remote fonts, analytics, or external runtime assets. The static server serves
only this directory/build; no episode data is sent to it.

## Stack and local commands

Vanilla TypeScript + Vite; native IndexedDB and a small generated service worker.
There are no production library dependencies. This is enough for two screens and
keeps the offline shell inspectable. Vitest + fake-indexeddb cover the model and
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

IndexedDB database **`zymptom-pwa-m0-disposable`**, version 1, is scoped to the
browser origin/profile. `episodes` holds local record heads and tombstones;
`installation` holds one persistent random device UUID. `EpisodeStore` owns
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

It requires confirmation, clears only M0 episode heads/tombstones, and preserves
the installation identity. This helper is absent from production builds. For a
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
No runtime import/export feature is provided.

Generate or check the references without opening a journal or device config:

```bash
npm run check:desktop
# Intentional refresh, after a reviewed desktop contract change:
../.venv/bin/python scripts/desktop-fixtures.py
../.venv/bin/python scripts/branding-icons.py
```

Local heads carry `record_id`, `record_type`, `device_id`, `local_revision`,
`local_change_id`, `previous_local_change_id`, `updated_at`, and `deleted`.
**These are local edit counters/heads, not Z5 wire Changes.** M0 does not retain a
complete immutable DAG, compute Z5 content hashes, distinguish meaningful sync
revisions from local saves, merge conflicts, track provider acknowledgements,
or implement backup-before-merge. Full revision history, wire validation,
backup safety and explicit conflict handling must be integrated before any later
transport can claim Z5 interoperability. Do not send these local wrappers to the
Python merge API or synchronize whole IndexedDB/SQLite files.

## Verification commands

```bash
npm test
npm run build
npm run check:desktop
npm run test:e2e
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
