#!/usr/bin/env bash
# Disposable Android test route. It serves only this PWA build on loopback.
set -uo pipefail

PWA_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=4173
SERVER_PID=""
SERVER_LOG=""
ADB_BIN="${ADB:-adb}"
DEVICE=""
REVERSE_ADDED=0

fail() {
  printf '\nAndroid PWA test could not start: %s\n' "$1" >&2
  exit 1
}

if [[ ! -x "$PWA_DIR/node_modules/.bin/vite" || ! -x "$PWA_DIR/node_modules/.bin/tsc" ]]; then
  fail "PWA dependencies are missing. From $PWA_DIR run 'npm ci', then rerun 'npm run phone:android'."
fi

if [[ "$ADB_BIN" == */* ]]; then
  [[ -x "$ADB_BIN" ]] || fail "ADB was not found at '$ADB_BIN'. Install Android Platform Tools (on Linux Mint/LMDE: 'sudo apt install adb'), then rerun this command."
else
  ADB_BIN="$(command -v -- "$ADB_BIN" 2>/dev/null || true)"
  [[ -n "$ADB_BIN" ]] || fail "Android Platform Tools (adb) are required. On Linux Mint/LMDE, install them with 'sudo apt install adb', then rerun 'npm run phone:android'."
fi

"$ADB_BIN" start-server >/dev/null 2>&1 || fail "ADB could not start. Check the Android Platform Tools installation and USB permissions."
DEVICES="$("$ADB_BIN" devices -l 2>/dev/null)" || fail "ADB could not list devices. Unplug/reconnect the phone and retry."

if [[ -n "${ANDROID_SERIAL:-}" ]]; then
  DEVICE_STATUS="$(printf '%s\n' "$DEVICES" | awk -v serial="$ANDROID_SERIAL" '$1 == serial { print $2; exit }')"
  case "$DEVICE_STATUS" in
    device) DEVICE="$ANDROID_SERIAL" ;;
    unauthorized) fail "Your phone is connected but has not authorized USB debugging. Unlock it, accept the 'Allow USB debugging?' prompt, then rerun this command." ;;
    offline) fail "ADB sees your phone as offline. Reconnect the USB cable, unlock the phone, and rerun this command." ;;
    *) fail "ANDROID_SERIAL '$ANDROID_SERIAL' is not an authorized connected phone. Run 'adb devices -l' and select a device listed as 'device'." ;;
  esac
else
  mapfile -t READY_DEVICES < <(printf '%s\n' "$DEVICES" | awk 'NF >= 2 && $2 == "device" { print $1 }')
  mapfile -t UNAUTHORIZED_DEVICES < <(printf '%s\n' "$DEVICES" | awk 'NF >= 2 && $2 == "unauthorized" { print $1 }')
  mapfile -t OFFLINE_DEVICES < <(printf '%s\n' "$DEVICES" | awk 'NF >= 2 && $2 == "offline" { print $1 }')
  if (( ${#UNAUTHORIZED_DEVICES[@]} > 0 )); then
    fail "Your phone is connected but USB debugging is not authorized. Unlock the phone, accept the 'Allow USB debugging?' prompt, then rerun this command."
  fi
  if (( ${#OFFLINE_DEVICES[@]} > 0 )); then
    fail "ADB sees a phone as offline. Reconnect the USB cable, unlock the phone, and rerun this command."
  fi
  if (( ${#READY_DEVICES[@]} == 0 )); then
    fail "No authorized Android phone found. Connect the phone with USB, enable Developer Options and USB debugging, accept the authorization prompt on the phone, and rerun this command."
  fi
  if (( ${#READY_DEVICES[@]} > 1 )); then
    fail "${#READY_DEVICES[@]} Android devices are connected. Leave one phone connected, or choose one with 'ANDROID_SERIAL=<device-id> npm run phone:android'. Use 'adb devices -l' to see device IDs."
  fi
  DEVICE="${READY_DEVICES[0]}"
fi

if ! node -e 'const net = require("node:net"); const server = net.createServer(); server.once("error", () => process.exit(1)); server.listen(Number(process.argv[1]), "127.0.0.1", () => server.close(() => process.exit(0)));' "$PORT" >/dev/null 2>&1; then
  fail "Port $PORT is already in use. Stop the other preview with Ctrl+C in its terminal, then rerun 'npm run phone:android'."
fi

printf 'Building the PWA…\n'
if ! (cd "$PWA_DIR" && npm run build); then
  fail "The PWA build failed. Fix the reported build error, then rerun this command."
fi

EXISTING_REVERSE="$("$ADB_BIN" -s "$DEVICE" reverse --list 2>/dev/null || true)"
if printf '%s\n' "$EXISTING_REVERSE" | grep -Eq "(^|[[:space:]])tcp:${PORT}([[:space:]]|$)"; then
  fail "An ADB reverse mapping already uses port $PORT. Remove it with 'adb -s $DEVICE reverse --remove tcp:$PORT', then rerun this command."
fi

if ! "$ADB_BIN" -s "$DEVICE" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1; then
  fail "ADB could not forward port $PORT. Reconnect and authorize the phone, then rerun this command."
fi
REVERSE_ADDED=1

cleanup() {
  local result=$?
  trap - EXIT INT TERM
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if (( REVERSE_ADDED )); then
    if "$ADB_BIN" -s "$DEVICE" reverse --remove "tcp:$PORT" >/dev/null 2>&1; then
      printf '\nStopped the preview and removed its ADB port forward.\n'
    else
      printf '\nCould not remove the ADB port forward automatically. Run: adb -s %s reverse --remove tcp:%s\n' "$DEVICE" "$PORT" >&2
    fi
  fi
  if [[ -n "$SERVER_LOG" ]]; then rm -f -- "$SERVER_LOG"; fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

SERVER_LOG="$(mktemp "${TMPDIR:-/tmp}/zymptom-pwa-preview.XXXXXX")" || fail "Could not create a temporary preview log. Check writable temporary storage."
(cd "$PWA_DIR" && exec ./node_modules/.bin/vite preview --host 127.0.0.1 --port "$PORT" --strictPort) >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

URL="http://localhost:$PORT/"
READY=0
for attempt in {1..20}; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  if node -e 'fetch(process.argv[1]).then((response) => process.exit(response.ok ? 0 : 1)).catch(() => process.exit(1))' "$URL" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if (( ! READY )); then
  if grep -q 'EADDRINUSE' "$SERVER_LOG"; then
    printf '\nAndroid PWA test could not start: Port %s became occupied. Stop the other preview with Ctrl+C in its terminal and rerun.\n' "$PORT" >&2
  else
    printf '\nAndroid PWA test could not start: The preview did not become ready.\n' >&2
    tail -n 5 "$SERVER_LOG" >&2
  fi
  exit 1
fi

printf '\n============================================================\n'
printf '  OPEN THIS URL IN CHROME ON YOUR ANDROID PHONE:\n\n  %s\n' "$URL"
printf '\n  Wait for “Offline app shell ready”, then install or test.\n'
printf '  Keep this terminal open. Press Ctrl+C when finished.\n'
printf '============================================================\n'
wait "$SERVER_PID"
