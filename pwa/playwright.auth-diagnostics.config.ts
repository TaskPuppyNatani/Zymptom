import { defineConfig, devices } from '@playwright/test';

const useExistingBuild = process.env.AUTH_DIAGNOSTICS_USE_EXISTING_BUILD === 'true';

// Compile the real app with minification and disposable public configuration.
// A separate output/port leaves the phone preview and its configuration intact.
export default defineConfig({
  testDir: './tests/auth-browser',
  outputDir: './test-results/auth-diagnostics-results',
  workers: 1,
  use: { ...devices['Desktop Chrome'], baseURL: 'http://localhost:4174', channel: 'chrome' },
  webServer: {
    command: useExistingBuild
      ? 'npm run preview -- --port 4174'
      : 'npm exec -- vite build --outDir test-results/auth-diagnostics-app && npm run preview -- --outDir test-results/auth-diagnostics-app --port 4174',
    url: 'http://localhost:4174',
    reuseExistingServer: false,
    env: {
      VITE_FIREBASE_API_KEY: 'demo-api-key',
      VITE_FIREBASE_AUTH_DOMAIN: 'demo-zymptom-z51.firebaseapp.com',
      VITE_FIREBASE_PROJECT_ID: 'demo-zymptom-z51',
      VITE_FIREBASE_APP_ID: '1:0:web:demo',
      VITE_FIREBASE_STORAGE_BUCKET: '',
      VITE_FIREBASE_MESSAGING_SENDER_ID: '',
      VITE_FIREBASE_USE_EMULATORS: 'false',
    },
  },
  reporter: 'list',
});
