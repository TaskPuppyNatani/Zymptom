import { defineConfig, devices } from '@playwright/test';

// Isolated local emulator and port; never reuse the phone's preview server.
export default defineConfig({
  testDir: './tests/persistence-browser',
  outputDir: './test-results/persistence-browser',
  workers: 1,
  use: { ...devices['Desktop Chrome'], baseURL: 'http://localhost:4175', channel: 'chrome' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4175 --strictPort',
    url: 'http://127.0.0.1:4175',
    reuseExistingServer: false,
    env: {
      VITE_FIREBASE_API_KEY: 'demo-api-key', VITE_FIREBASE_AUTH_DOMAIN: 'localhost',
      VITE_FIREBASE_PROJECT_ID: 'demo-zymptom-z51', VITE_FIREBASE_APP_ID: '1:0:web:demo',
      VITE_FIREBASE_STORAGE_BUCKET: '', VITE_FIREBASE_MESSAGING_SENDER_ID: '',
      VITE_FIREBASE_USE_EMULATORS: 'true',
    },
  },
  reporter: 'list',
});
