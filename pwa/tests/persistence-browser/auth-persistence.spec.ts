import { expect, test } from '@playwright/test';

test('Firebase local auth restores a joined journal after reopening and disconnect persists', async ({ page, context }) => {
  await page.goto('/');
  await page.evaluate(async () => {
    const fixture = await import('/tests/persistence-browser/fixture.ts');
    await fixture.prepareSyntheticJournal();
  });
  // Reload to discover the synthetic desktop descriptor created in the emulator.
  await page.reload();
  const panel = page.locator('.production-panel');
  await panel.locator('#production-sync-heading').click();
  await expect(panel.getByRole('button', { name: 'Join existing journal' })).toBeEnabled();
  await panel.getByRole('button', { name: 'Join existing journal' }).click();
  await expect(panel.getByRole('button', { name: 'Sync now' })).toBeEnabled();

  await page.getByRole('button', { name: '＋ Log Something' }).click();
  await page.getByLabel('What happened?', { exact: true }).fill('Synthetic unsynced entry');
  await page.getByLabel('Outcome', { exact: true }).selectOption('stopped_early');
  await page.getByRole('button', { name: 'Save entry', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit Synthetic unsynced entry' })).toBeVisible();
  await page.close();

  const reopened = await context.newPage();
  let writes = 0;
  reopened.on('request', (request) => { if (request.url().includes('firestore') && request.url().includes(':commit')) writes++; });
  await reopened.goto('/');
  const restored = reopened.locator('.production-panel');
  await restored.locator('#production-sync-heading').click();
  await expect(restored.getByRole('button', { name: 'Sync now' })).toBeEnabled();
  await expect(restored).toContainText('Connected with Google');
  await expect(reopened.getByRole('button', { name: 'Edit Synthetic unsynced entry' })).toBeVisible();
  expect(await reopened.evaluate(async () => (await import('/tests/persistence-browser/fixture.ts')).cloudChangeCount())).toBe(0);
  expect(writes).toBe(0);
  await restored.getByRole('button', { name: 'Disconnect' }).click();
  await expect(restored).toContainText('Not connected');
  await reopened.close();

  const signedOut = await context.newPage();
  await signedOut.goto('/');
  const signedOutPanel = signedOut.locator('.production-panel');
  await signedOutPanel.locator('#production-sync-heading').click();
  await expect(signedOutPanel.getByRole('button', { name: 'Connect with Google' })).toBeEnabled();
  await expect(signedOut.getByRole('button', { name: 'Edit Synthetic unsynced entry' })).toBeVisible();
  expect(await signedOut.evaluate(async () => (await import('/tests/persistence-browser/fixture.ts')).joinedJournal()))
    .toBe('11111111-2222-4333-8444-555555555555');
});
