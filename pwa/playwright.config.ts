import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  workers: 1,
  use: { ...devices['Pixel 7'], baseURL: 'http://localhost:4173', channel: 'chrome', timezoneId: 'America/Los_Angeles' },
  webServer: { command: 'npm run preview', url: 'http://localhost:4173', reuseExistingServer: false },
  reporter: 'list',
});
