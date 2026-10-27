# Native desktop Sandbox Sync

This is a **disposable proof**, not production journal sync. Use `DEMO:` entries
only. The explicit launch opens a separately owned database at
`$XDG_DATA_HOME/zymptom/sandbox-sync/journal.sqlite3` (normally
`~/.local/share/zymptom/sandbox-sync/journal.sqlite3`). It refuses unowned,
aliased or replaced database files and cannot fall back to the normal journal.
Restore is disabled in this mode. Safety backups for merges stay beside this
disposable database. Do not use the sandbox directory for a real journal.

## One-time desktop OAuth setup

The Firebase sandbox and PWA configuration already in use stay unchanged.

1. In the existing Google Cloud project, open **APIs & Services → Credentials**,
   choose **Create credentials → OAuth client ID**, and select **Desktop app**.
   Create it and copy its client ID and, if provided, client secret. If Google's
   consent screen is in testing mode, the sandbox account must be an allowed test
   user.
2. Copy [the example](sandbox-sync.example.json) to
   `$XDG_CONFIG_HOME/zymptom/sandbox-sync/firebase.json` (normally
   `~/.config/zymptom/sandbox-sync/firebase.json`). Fill in the Desktop client ID
   and secret, plus the existing Firebase **Web API key** and **project ID** from
   the PWA's local configuration. If Google provides no secret, use an empty
   string. Keep the file private and local; do not commit it. On Linux, use
   `chmod 600 ~/.config/zymptom/sandbox-sync/firebase.json` (adjust the path if
   using a custom XDG config directory).

Google sign-in opens the system browser and returns through a temporary listener
on `127.0.0.1`. The desktop uses OAuth state and PKCE and exchanges the Google
credential for a Firebase user token. Tokens are held in memory only; sign in
again after restarting or expiry. Cancel sign-in in the window if needed.
See Google's [desktop OAuth guidance](https://developers.google.com/identity/protocols/oauth2/native-app)
and [Firebase Auth REST reference](https://firebase.google.com/docs/reference/rest/auth).

## Launch and sync

From the repository root:

```bash
.venv/bin/python -m zymptom --sandbox-sync
```

Confirm the **SANDBOX SYNC · DISPOSABLE DATA** banner. Sign in with the same
Google account as the Android PWA and select **Sync disposable entries**. Sync
is manual. Conflicting changes remain separate; inspect the developer conflict
panel, explicitly select a current candidate, and sync again to share the
resolution. Local logging remains available while signed out.

For the live proof, create `DEMO: Native desktop sync` on desktop, sync desktop
then phone, and check it appears on the phone. Create `DEMO: Phone to native`
on the phone, sync phone then desktop, and check Today or History. Edit one shared
entry, sync both, and check the edit. Delete a shared entry, sync both, and
check it disappears on the other device. Use only disposable entries.

The transport uses the existing Firebase UID-isolated immutable mailbox and user
ID token; it does not use administrative credentials or change Firestore rules.
The native merge service verifies a safety backup before applying incoming
Changes. Neither the production desktop database nor its records are synced.
