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
