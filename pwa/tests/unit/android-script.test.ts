import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { appendFile, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

const script = new URL('../../scripts/test-android.sh', import.meta.url);
const tempDirectories: string[] = [];
const fakeAdbSource = `#!/usr/bin/env bash
printf '%s\n' "$*" >> "$ADB_TEST_LOG"
if [[ "$1" == "start-server" ]]; then exit 0; fi
if [[ "$1" == "devices" ]]; then
  printf 'List of devices attached\n'
  case "$ANDROID_ADB_MODE" in
    ready|existing-reverse) printf 'test-phone\tdevice product:synthetic\n' ;;
    unauthorized) printf 'test-phone\tunauthorized usb:1-1\n' ;;
    offline) printf 'test-phone\toffline usb:1-1\n' ;;
    multiple) printf 'test-phone\tdevice product:synthetic\ntest-phone-2\tdevice product:synthetic\n' ;;
  esac
  exit 0
fi
if [[ "$1" == "-s" && "$3" == "reverse" ]]; then
  case "$4" in
    --list) [[ "$ANDROID_ADB_MODE" == "existing-reverse" ]] && printf 'test-phone tcp:4173 tcp:4173\n'; exit 0 ;;
    --remove) printf 'reverse-removed %s\n' "$5" >> "$ADB_TEST_LOG"; exit 0 ;;
    tcp:4173)
      [[ "$5" == "tcp:4173" ]] || exit 1
      exit 0 ;;
  esac
fi
exit 2
`;

afterEach(async () => {
  await Promise.all(tempDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function fixture(mode: string) {
  const directory = await mkdtemp(join(tmpdir(), 'zymptom-android-script-'));
  tempDirectories.push(directory);
  const adb = join(directory, 'adb');
  const log = join(directory, 'adb.log');
  await writeFile(adb, fakeAdbSource, { mode: 0o700 });
  return { directory, adb, log, env: { ...process.env, ADB: adb, ADB_TEST_LOG: log, ANDROID_ADB_MODE: mode } };
}

function invoke(adb: string, env: NodeJS.ProcessEnv) {
  return spawnSync('bash', [adb], { cwd: new URL('../..', script), env, encoding: 'utf8', timeout: 5000 });
}

describe('Android test command', () => {
  it('explains how to get adb when Android Platform Tools are missing', async () => {
    const value = await fixture('ready');
    const result = invoke(new URL('../../scripts/test-android.sh', import.meta.url).pathname, { ...value.env, ADB: join(value.directory, 'no-such-adb') });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('sudo apt install adb');
    expect(result.stderr).toContain('Platform Tools');
    expect(result.stderr).not.toContain('stack trace');
  });

  it.each([
    ['none', 'No authorized Android phone found', 'enable Developer Options'],
    ['unauthorized', 'not authorized', 'Allow USB debugging?'],
    ['offline', 'offline', 'Reconnect the USB cable'],
    ['multiple', '2 Android devices are connected', 'ANDROID_SERIAL='],
    ['existing-reverse', 'reverse mapping already uses port 4173', 'reverse --remove tcp:4173'],
  ])('reports the %s device state without starting the preview', async (mode, message, nextStep) => {
    const value = await fixture(mode);
    const result = invoke(new URL('../../scripts/test-android.sh', import.meta.url).pathname, value.env);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain(message);
    expect(result.stderr).toContain(nextStep);
    expect(result.stderr).not.toContain('Error:');
    const log = await readFile(value.log, 'utf8');
    expect(log).not.toContain('reverse tcp:4173 tcp:4173');
  });

  it('builds, forwards to the preview, prints the phone URL and cleans up on Ctrl+C', async () => {
    const value = await fixture('ready');
    const child: ChildProcess = spawn('bash', [new URL('../../scripts/test-android.sh', import.meta.url).pathname], {
      cwd: new URL('../..', script), env: value.env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout!.setEncoding('utf8').on('data', (text: string) => { stdout += text; });
    child.stderr!.setEncoding('utf8').on('data', (text: string) => { stderr += text; });
    const closed = new Promise<number | null>((resolve) => child.once('close', resolve));
    const deadline = setTimeout(() => child.kill('SIGKILL'), 25_000);
    try {
      for (let attempt = 0; attempt < 250 && !stdout.includes('http://localhost:4173/'); attempt++) {
        if (child.exitCode !== null) break;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      expect(stdout, stderr).toContain('http://localhost:4173/');
      const response = await fetch('http://localhost:4173/manifest.webmanifest');
      expect(response.ok).toBe(true);
      expect(await response.text()).toContain('"name": "Zymptom"');
      await appendFile(value.log, 'preview-served-through-mock-reverse\n');
      child.kill('SIGINT');
      expect(await closed).toBe(130);
      expect(stdout).toContain('removed its ADB port forward');
      const releasedPort = net.createServer();
      await new Promise<void>((resolve, reject) => {
        releasedPort.once('error', reject);
        releasedPort.listen(4173, '127.0.0.1', resolve);
      });
      await new Promise<void>((resolve) => releasedPort.close(() => resolve()));
      const log = await readFile(value.log, 'utf8');
      expect(log).toContain('reverse tcp:4173 tcp:4173');
      expect(log).toContain('preview-served-through-mock-reverse');
      expect(log).toContain('reverse-removed tcp:4173');
    } finally {
      clearTimeout(deadline);
      if (child.exitCode === null) child.kill('SIGKILL');
    }
  }, 30_000);

  it('explains a port collision without changing a listener or installing a forward', async () => {
    const value = await fixture('ready');
    const server = net.createServer();
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(4173, '127.0.0.1', resolve);
    });
    try {
      const result = invoke(new URL('../../scripts/test-android.sh', import.meta.url).pathname, value.env);
      expect(result.status).toBe(1);
      expect(result.stderr).toContain('Port 4173 is already in use');
      expect(result.stderr).toContain('Ctrl+C');
      expect(await readFile(value.log, 'utf8')).not.toContain('reverse tcp:4173 tcp:4173');
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});
