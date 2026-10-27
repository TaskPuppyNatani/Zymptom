import { expect, test, type Page } from '@playwright/test';

async function openPanel(page: Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
  await expect(page.locator('#shell-status')).toContainText('Offline app shell ready');
  const panel = page.locator('.sandbox-panel');
  await panel.locator('#sandbox-sync-heading').click();
  return panel;
}

async function signInWithEmulator(page: Page) {
  const popupPromise = page.waitForEvent('popup');
  await page.locator('.sandbox-panel').getByRole('button', { name: 'Connect with Google', exact: true }).click();
  const popup = await popupPromise;
  await popup.waitForLoadState('domcontentloaded');
  const body = popup.locator('body');
  await expect(body).toContainText(/Google|Firebase/i);
  const candidateButtons = popup.getByRole('button');
  if (await candidateButtons.count()) {
    const labels = await candidateButtons.evaluateAll((buttons) => buttons.map((button) =>
      `${button.textContent ?? ''} ${button.getAttribute('aria-label') ?? ''}`));
    const action = labels.findIndex((label) => /sign in|continue|confirm|authorize/i.test(label));
    if (action >= 0) await candidateButtons.nth(action).click();
  }
  const addAccount = popup.getByText('Add new account', { exact: true });
  if (await addAccount.count()) {
    await addAccount.click();
    await popup.getByLabel('Email').fill('z51-synthetic-user@example.test');
    await popup.getByRole('button', { name: 'Sign in with Google.com', exact: true }).click();
  }
  await expect(page.locator('.sandbox-panel .sandbox-state').first()).toHaveText('Connected', { timeout: 15_000 });
  await popup.close().catch(() => undefined);
}

test('Auth emulator Google sign-in restores, uploads only fixtures, and sign-out preserves local entries', async ({ page }) => {
  const panel = await openPanel(page);
  await page.getByRole('button', { name: '＋ Log Something' }).click();
  await page.getByLabel('What happened?', { exact: true }).fill('DEMO: Local entry stays local');
  await page.getByLabel('Outcome', { exact: true }).selectOption('stopped_early');
  await page.getByRole('button', { name: 'Save entry', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit DEMO: Local entry stays local', exact: true })).toBeVisible();
  await signInWithEmulator(page);
  await panel.locator('.sync-developer summary').click();
  await expect(panel.getByRole('button', { name: 'Upload Change A · synthetic create', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Upload Change A · synthetic create', exact: true }).click();
  await expect(panel.locator('.sync-developer .sandbox-state')).toContainText('Uploaded synthetic Change A');
  await panel.getByRole('button', { name: 'Pull sandbox packets', exact: true }).click();
  await expect(panel.locator('.sandbox-packets')).toContainText('00000000-0000-0000-0000-00000000012d');
  await expect(page.getByRole('button', { name: 'Edit DEMO: Local entry stays local', exact: true })).toBeVisible();

  // Disable the network while a Firebase account is signed in. Local CRUD stays independent.
  await page.context().setOffline(true);
  await page.getByRole('button', { name: '＋ Log Something' }).click();
  await page.getByLabel('What happened?', { exact: true }).fill('DEMO: Offline while sandbox signed in');
  await page.getByLabel('Outcome', { exact: true }).selectOption('completed_with_difficulty');
  await page.getByRole('button', { name: 'Save entry', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit DEMO: Offline while sandbox signed in', exact: true })).toBeVisible();
  await page.context().setOffline(false);

  await page.reload();
  const restoredPanel = page.locator('.sandbox-panel');
  await restoredPanel.locator('#sandbox-sync-heading').click();
  await expect(restoredPanel.locator('.sandbox-state').first()).toHaveText('Connected', { timeout: 15_000 });
  await expect(page.getByRole('button', { name: 'Edit DEMO: Local entry stays local', exact: true })).toBeVisible();
  await restoredPanel.getByRole('button', { name: 'Disconnect', exact: true }).click();
  await expect(restoredPanel.locator('.sandbox-state').first()).toHaveText('Not connected');
  await expect(page.getByRole('button', { name: 'Edit DEMO: Local entry stays local', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Edit DEMO: Offline while sandbox signed in', exact: true })).toBeVisible();
});

test('two isolated PWA installations sync entries, preserve conflicts, resolve and propagate delete', async ({ page, browser }) => {
  test.setTimeout(90_000);
  const otherContext = await browser.newContext({ baseURL: 'http://localhost:4173', timezoneId: 'America/Los_Angeles' });
  const other = await otherContext.newPage();
  page.setDefaultTimeout(10_000);
  other.setDefaultTimeout(10_000);
  try {
    const panelA = await openPanel(page);
    const panelB = await openPanel(other);
    await expect(panelA.locator('.sync-developer')).not.toHaveAttribute('open', '');
    await expect(panelA.getByRole('button', { name: 'Review conflict' })).toHaveCount(0);
    await signInWithEmulator(page);
    await signInWithEmulator(other);
    await page.getByRole('button', { name: '＋ Log Something' }).click();
    await page.getByLabel('What happened?', { exact: true }).fill('DEMO: Shared browser entry');
    await page.getByLabel('Outcome', { exact: true }).selectOption('stopped_early');
    await page.getByRole('button', { name: 'Save entry', exact: true }).click();
    const sync = async (panel: typeof panelA) => {
      await panel.getByRole('button', { name: 'Sync now', exact: true }).click();
      await expect(panel.getByRole('button', { name: 'Sync now', exact: true })).toBeEnabled({ timeout: 15_000 });
    };
    await test.step('exchange the initial create', async () => { await sync(panelA); await sync(panelB); });
    await expect(panelA.locator('.sandbox-state').first()).toHaveText('Up to date');
    await expect(panelA.getByText(/^Google account:/)).toBeVisible();
    await expect(panelA.getByText(/^Last synced:/)).toBeVisible();
    await expect(other.getByRole('button', { name: 'Edit DEMO: Shared browser entry' })).toBeVisible();
    await test.step('make independent edits', async () => { for (const [current, notes] of [[page, 'Phone version'], [other, 'Desktop version']] as const) {
      await current.getByRole('button', { name: 'Edit DEMO: Shared browser entry' }).click();
      await current.getByLabel('Notes', { exact: true }).fill(notes);
      await current.getByRole('button', { name: 'Save entry', exact: true }).click();
    } });
    await expect(panelA.locator('.sandbox-state').first()).toHaveText('Connected');
    await expect(panelB.locator('.sandbox-state').first()).toHaveText('Connected');
    await test.step('exchange concurrent edits', async () => { await sync(panelA); await sync(panelB); });
    await expect(panelB.locator('.sandbox-state').first()).toHaveText('Needs attention');
    await panelB.getByRole('button', { name: 'Review conflict' }).click();
    await expect(panelB.getByText('This entry changed differently on two devices. Choose the version you want to keep.')).toBeVisible();
    await expect(panelB).toContainText('Phone version');
    await expect(panelB).toContainText('Desktop version');
    await expect(panelB.locator('.sandbox-conflicts')).not.toContainText('change_id');
    await panelB.getByRole('button', { name: 'Keep this version', exact: true }).click();
    await expect(panelB.getByRole('button', { name: 'Review conflict' })).toHaveCount(0);
    await test.step('exchange the resolution', async () => { await sync(panelB); await sync(panelA); });
    await expect(panelA.locator('.sandbox-state').first()).toHaveText('Up to date');
    await other.getByRole('button', { name: 'Edit DEMO: Shared browser entry' }).click();
    await other.getByRole('button', { name: 'Delete entry…' }).click();
    await other.getByRole('dialog').getByRole('button', { name: 'Delete entry', exact: true }).click();
    await test.step('exchange the tombstone', async () => { await sync(panelB); await sync(panelA); });
    await expect(page.getByRole('button', { name: 'Edit DEMO: Shared browser entry' })).toHaveCount(0);
    await otherContext.setOffline(true);
    await other.getByRole('button', { name: '＋ Log Something' }).click();
    await other.getByLabel('What happened?', { exact: true }).fill('DEMO: Offline still works');
    await other.getByLabel('Outcome', { exact: true }).selectOption('stopped_early');
    await other.getByRole('button', { name: 'Save entry', exact: true }).click();
    await expect(other.getByRole('button', { name: 'Edit DEMO: Offline still works' })).toBeVisible();
    await panelB.getByRole('button', { name: 'Sync now', exact: true }).click();
    await expect(panelB.locator('.sandbox-state').first()).toHaveText('Offline', { timeout: 20_000 });
    await expect(panelB.getByRole('alert').first()).toContainText('entries are safe');
  } finally { await otherContext.close(); }
});
