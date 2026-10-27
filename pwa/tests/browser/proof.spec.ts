import { chromium, expect, test, type Page } from '@playwright/test';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

async function ready(page: Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.isSecureContext)).toBe(true);
  await expect(page.locator('#shell-status')).toHaveText('Offline app shell ready. Entries are stored on this browser.');
}
async function create(page: Page, activity = 'DEMO: Making lunch') {
  await page.getByRole('button', { name: '＋ Log Something' }).click();
  await page.getByLabel('What happened?', { exact: true }).fill(activity);
  await page.getByLabel('Outcome', { exact: true }).selectOption('completed_with_difficulty');
  await page.getByRole('button', { name: 'Save entry', exact: true }).click();
  await expect(page.getByRole('button', { name: `Edit ${activity}`, exact: true })).toBeVisible();
}
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

test('Chromium finds an installable manifest and an active, complete offline shell', async ({ page, context }) => {
  await ready(page);
  const cdp = await context.newCDPSession(page);
  const manifest = await cdp.send('Page.getAppManifest');
  expect(manifest.errors).toEqual([]);
  expect(JSON.parse(manifest.data).name).toBe('Zymptom');
  const installation = await cdp.send('Page.getInstallabilityErrors');
  // Playwright's isolated test contexts are incognito, where Chrome disables
  // installation. The persistent-profile test below requires zero errors.
  expect(installation.installabilityErrors.filter((error) => error.errorId !== 'in-incognito')).toEqual([]);
  const keys = await page.evaluate(() => caches.keys());
  expect(keys.some((key) => key.startsWith('zymptom-m0-shell-'))).toBe(true);
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
  await expect(page.locator('#shell-status')).toContainText('Offline app shell ready');
});

test('local create, cancel, edit and confirmed delete work offline without a data request', async ({ page, context }) => {
  await ready(page);
  const requests: string[] = [];
  page.on('request', (request) => requests.push(`${request.method()} ${request.url()}`));
  await context.setOffline(true);
  const activity = 'DEMO: Café ☕\n雪';
  await create(page, activity);
  await create(page, 'DEMO: Keep this other entry');
  await page.getByRole('button', { name: `Edit ${activity}`, exact: true }).click();
  await page.getByLabel('Duration · minutes', { exact: true }).fill('0');
  await page.getByLabel('Notes', { exact: true }).fill('Multiline\nKeyboard text 雪');
  await page.getByText('More details', { exact: true }).click();
  await page.getByLabel('Recovery required?', { exact: true }).selectOption('no');
  await page.getByLabel('Recovery · minutes', { exact: true }).fill('');
  await page.getByRole('button', { name: 'Save entry', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: `Edit ${activity}`, exact: true })).toContainText('0 min');
  await page.getByRole('button', { name: `Edit ${activity}`, exact: true }).click();
  await expect(page.getByLabel('Notes', { exact: true })).toHaveValue('Multiline\nKeyboard text 雪');
  await expect(page.getByLabel('Duration · minutes', { exact: true })).toHaveValue('0');
  await page.getByText('More details', { exact: true }).click();
  await expect(page.getByLabel('Recovery required?', { exact: true })).toHaveValue('no');
  await expect(page.getByLabel('Recovery · minutes', { exact: true })).toHaveValue('');
  await page.getByLabel('What happened?', { exact: true }).fill('Unsaved change');
  await page.getByRole('button', { name: 'Delete entry…', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(activity);
  await expect(dialog).toContainText(new Intl.DateTimeFormat('en-US', { year: 'numeric' }).format(new Date()));
  await dialog.getByRole('button', { name: 'Keep entry', exact: true }).click();
  await expect(page.getByLabel('What happened?', { exact: true })).toHaveValue('Unsaved change');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('button', { name: `Edit ${activity}`, exact: true })).toBeVisible();
  await page.getByRole('button', { name: `Edit ${activity}`, exact: true }).click();
  await page.getByRole('button', { name: 'Delete entry…', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete entry', exact: true }).click();
  await expect(page.getByRole('button', { name: `Edit ${activity}`, exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Edit DEMO: Keep this other entry', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Edit DEMO: Keep this other entry', exact: true })).toBeVisible();
  expect(requests.filter((request) => !request.startsWith('GET http://localhost:4173/'))).toEqual([]);
  expect(requests.some((request) => /sqlite|journal|api\/|sync\//i.test(request))).toBe(false);
});

test('all five multi-value controls add selections in touch order and prevent repeated/mixed duplicates', async ({ page }) => {
  await ready(page);
  await page.getByRole('button', { name: '＋ Log Something' }).click();
  await expect(page.getByRole('button', { name: 'Delete entry…', exact: true })).toHaveCount(0);
  await page.getByLabel('What happened?', { exact: true }).fill('DEMO: Ordered choices');
  await page.getByLabel('Outcome', { exact: true }).selectOption('stopped_early');
  await page.getByText('More details', { exact: true }).click();
  const fields = [
    ['functional_demands', 'Walking', 'Sitting', 'Memory'],
    ['symptoms', 'Pain', 'Fatigue', 'Dizziness'],
    ['adaptations', 'Sat down', 'Changed position', 'Took break'],
    ['consequences', 'Needed rest', 'Missed next activity', 'Reduced activity afterward'],
    ['recovery_actions', 'Sensory recovery', 'Lay down', 'Medication'],
  ];
  for (const [field, first, second, third] of fields) {
    const group = page.locator(`fieldset[data-field="${field}"]`);
    await group.getByText('Choose values', { exact: true }).click();
    await group.getByRole('checkbox', { name: first, exact: true }).check();
    await group.getByRole('checkbox', { name: second, exact: true }).check();
    await group.getByRole('button', { name: 'Add selected', exact: true }).click();
    await expect(group.locator('.chip')).toHaveText([`${first} ×`, `${second} ×`]);
    await expect(group.getByRole('checkbox', { name: first, exact: true })).not.toBeChecked();
    await expect(group.getByRole('button', { name: 'Add selected', exact: true })).toBeDisabled();
    await group.getByRole('checkbox', { name: first, exact: true }).check();
    await group.getByRole('checkbox', { name: third, exact: true }).check();
    await group.getByRole('button', { name: 'Add selected', exact: true }).click();
    await expect(group.locator('.chip')).toHaveText([`${first} ×`, `${second} ×`, `${third} ×`]);
    await group.locator('.chip').first().click();
    await group.getByRole('checkbox', { name: first, exact: true }).check();
    await group.getByRole('button', { name: 'Add selected', exact: true }).click();
    await expect(group.locator('.chip')).toHaveText([`${second} ×`, `${third} ×`, `${first} ×`]);
  }
  await page.getByRole('button', { name: 'Save entry', exact: true }).click();
  await page.getByRole('button', { name: 'Edit DEMO: Ordered choices', exact: true }).click();
  await page.getByText('More details', { exact: true }).click();
  for (const [field, first, second, third] of fields) {
    await expect(page.locator(`fieldset[data-field="${field}"] .chip`)).toHaveText([`${second} ×`, `${third} ×`, `${first} ×`]);
  }
});

test('a stale deletion fails safely and preserves the editor draft', async ({ page, context }) => {
  await ready(page);
  await create(page, 'DEMO: Concurrent tab');
  await page.getByRole('button', { name: 'Edit DEMO: Concurrent tab', exact: true }).click();
  await page.getByLabel('Notes', { exact: true }).fill('Keep this unsaved draft');
  const other = await context.newPage();
  await ready(other);
  await other.getByRole('button', { name: 'Edit DEMO: Concurrent tab', exact: true }).click();
  await other.getByLabel('What happened?', { exact: true }).fill('DEMO: New saved state');
  await other.getByRole('button', { name: 'Save entry', exact: true }).click();
  await expect(other.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Delete entry…', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete entry', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('changed in another tab');
  await expect(page.getByLabel('Notes', { exact: true })).toHaveValue('Keep this unsaved draft');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit DEMO: New saved state', exact: true })).toBeVisible();
});

for (const width of [320, 360, 390, 430]) {
  test(`Today, form and delete dialog fit a ${width}px phone without horizontal overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await ready(page);
    await noOverflow(page);
    await create(page, `DEMO: ${'LongText'.repeat(20)}`);
    await noOverflow(page);
    await page.locator('.episode-card').click();
    await page.getByText('More details', { exact: true }).click();
    await page.locator('fieldset').first().getByText('Choose values', { exact: true }).click();
    await noOverflow(page);
    await page.getByRole('button', { name: 'Delete entry…', exact: true }).click();
    await noOverflow(page);
    const dialog = page.getByRole('dialog');
    expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  });
}

test('installed standalone app preserves entries, identity and offline shell across browser restart', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'zymptom-pwa-m0-profile-'));
  for (const name of ['data', 'config', 'cache']) await mkdir(join(directory, name));
  const options = { channel: 'chrome', headless: true, env: { ...process.env,
    XDG_DATA_HOME: join(directory, 'data'), XDG_CONFIG_HOME: join(directory, 'config'), XDG_CACHE_HOME: join(directory, 'cache') } };
  let context = await chromium.launchPersistentContext(join(directory, 'profile'), options);
  try {
    let page = context.pages()[0];
    await page.goto('http://localhost:4173/');
    await expect(page.locator('#shell-status')).toContainText('Offline app shell ready');
    const cdp = await context.newCDPSession(page);
    expect((await cdp.send('Page.getInstallabilityErrors')).installabilityErrors).toEqual([]);
    const manifestId = 'http://localhost:4173/';
    await cdp.send('PWA.install', { manifestId });
    await cdp.send('PWA.changeAppUserSettings', { manifestId, displayMode: 'standalone' });
    const [installed] = await Promise.all([context.waitForEvent('page'), cdp.send('PWA.launch', { manifestId })]);
    page = installed;
    await page.waitForLoadState();
    expect(await page.evaluate(() => matchMedia('(display-mode: standalone)').matches)).toBe(true);
    await create(page, 'DEMO: Across a browser restart');
    const ids = await page.evaluate(async () => {
      return await new Promise((resolve) => {
        const request = indexedDB.open('zymptom-pwa-m0-disposable');
        request.onsuccess = () => {
          const transaction = request.result.transaction(['episodes', 'installation']);
          const records = transaction.objectStore('episodes').getAll();
          const identity = transaction.objectStore('installation').get('device_id');
          transaction.oncomplete = () => { request.result.close(); resolve([records.result[0].record_id, identity.result]); };
        };
      });
    });
    await context.close();
    context = await chromium.launchPersistentContext(join(directory, 'profile'), options);
    await context.setOffline(true);
    page = context.pages()[0];
    await page.goto('http://localhost:4173/');
    await expect(page.getByRole('button', { name: 'Edit DEMO: Across a browser restart', exact: true })).toBeVisible();
    const restored = await page.evaluate(async () => new Promise((resolve) => {
      const request = indexedDB.open('zymptom-pwa-m0-disposable');
      request.onsuccess = () => {
        const transaction = request.result.transaction(['episodes', 'installation']);
        const records = transaction.objectStore('episodes').getAll();
        const identity = transaction.objectStore('installation').get('device_id');
        transaction.oncomplete = () => { request.result.close(); resolve([records.result[0].record_id, identity.result]); };
      };
    }));
    expect(restored).toEqual(ids);
    const reopenedCdp = await context.newCDPSession(page);
    const [reopenedApp] = await Promise.all([context.waitForEvent('page'), reopenedCdp.send('PWA.launch', { manifestId })]);
    await expect(reopenedApp.getByRole('button', { name: 'Edit DEMO: Across a browser restart', exact: true })).toBeVisible();
    expect(await reopenedApp.evaluate(() => matchMedia('(display-mode: standalone)').matches)).toBe(true);
    await reopenedCdp.send('PWA.uninstall', { manifestId });
  } finally { await context.close(); await rm(directory, { recursive: true, force: true }); }
});
