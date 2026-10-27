import { expect, test, type Page } from '@playwright/test';

async function openPanel(page: Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
  await expect(page.locator('#shell-status')).toContainText('Offline app shell ready');
  const panel = page.locator('.sandbox-panel');
  await panel.locator('summary').click();
  return panel;
}

async function signInWithEmulator(page: Page) {
  const popupPromise = page.waitForEvent('popup');
  await page.locator('.sandbox-panel').getByRole('button', { name: 'Sign in with Google', exact: true }).click();
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
  await expect(page.locator('.sandbox-panel .sandbox-state').first()).toContainText('Signed in to Sandbox Sync.', { timeout: 15_000 });
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
  await expect(panel.getByRole('button', { name: 'Upload Change A · synthetic create', exact: true })).toBeEnabled();
  await panel.getByRole('button', { name: 'Upload Change A · synthetic create', exact: true }).click();
  await expect(panel.locator('.sandbox-state').nth(1)).toContainText('Uploaded synthetic Change A');
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
  await restoredPanel.locator('summary').click();
  await expect(restoredPanel.locator('.sandbox-state').first()).toContainText('Signed in to Sandbox Sync.', { timeout: 15_000 });
  await expect(page.getByRole('button', { name: 'Edit DEMO: Local entry stays local', exact: true })).toBeVisible();
  await restoredPanel.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(restoredPanel.locator('.sandbox-state').first()).toContainText('Signed out.');
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
    await signInWithEmulator(page);
    await signInWithEmulator(other);
    await page.getByRole('button', { name: '＋ Log Something' }).click();
    await page.getByLabel('What happened?', { exact: true }).fill('DEMO: Shared browser entry');
    await page.getByLabel('Outcome', { exact: true }).selectOption('stopped_early');
    await page.getByRole('button', { name: 'Save entry', exact: true }).click();
    const sync = async (panel: typeof panelA) => {
      await panel.getByRole('button', { name: 'Sync disposable entries', exact: true }).click();
      await expect(panel.locator('.sandbox-state').nth(1)).toContainText('Sync complete.', { timeout: 15_000 });
    };
    await test.step('exchange the initial create', async () => { await sync(panelA); await sync(panelB); });
    await expect(other.getByRole('button', { name: 'Edit DEMO: Shared browser entry' })).toBeVisible();
    await test.step('make independent edits', async () => { for (const [current, notes] of [[page, 'Phone version'], [other, 'Desktop version']] as const) {
      await current.getByRole('button', { name: 'Edit DEMO: Shared browser entry' }).click();
      await current.getByLabel('Notes', { exact: true }).fill(notes);
      await current.getByRole('button', { name: 'Save entry', exact: true }).click();
    } });
    await test.step('exchange concurrent edits', async () => { await sync(panelA); await sync(panelB); });
    await expect(panelB.getByText('Entry needs explicit conflict resolution')).toBeVisible();
    await expect(panelB.getByText('Unresolved conflicts: 1 · developer proof')).toBeVisible();
    const inspect = panelB.getByText('Inspect this candidate');
    await inspect.first().click(); await inspect.last().click();
    await expect(panelB).toContainText('Phone version');
    await expect(panelB).toContainText('Desktop version');
    await panelB.getByRole('button', { name: /^Choose DEMO: Shared browser entry/ }).first().click();
    await expect(panelB.getByText('Unresolved conflicts: 0 · developer proof')).toBeVisible();
    await test.step('exchange the resolution', async () => { await sync(panelB); await sync(panelA); });
    await expect(panelA.getByText('Unresolved conflicts: 0 · developer proof')).toBeVisible();
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
  } finally { await otherContext.close(); }
});
