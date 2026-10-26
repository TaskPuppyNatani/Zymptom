import { expect, test } from '@playwright/test';

const privateUrl = 'https://private.example/credential?access_token=never-display';
const privateToken = 'never-display-token-value';

const cases = [
  {
    title: 'canonical unauthorized-domain Auth code',
    backendMessage: null,
    code: 'auth/unauthorized-domain',
  },
  {
    title: 'SDK-generated API-key code containing periods',
    backendMessage: 'API key not valid. Please pass a valid API key.',
    code: 'auth/api-key-not-valid.-please-pass-a-valid-api-key.',
  },
  {
    title: 'tagged FirebaseError carrying private customData',
    backendMessage: 'FEDERATED_USER_ID_ALREADY_LINKED',
    code: 'auth/credential-already-in-use',
  },
];

for (const scenario of cases) {
  test(`production sign-in catch renders ${scenario.title}`, async ({ page }) => {
    // Select the same redirect branch as an installed Android PWA, while keeping
    // desktop Chrome's SDK initialization deterministic. No SDK functions or
    // application formatters are mocked: the real SDK constructs FirebaseError.
    await page.addInitScript(() => Object.defineProperty(navigator, 'standalone', { value: true }));
    let projectRequests = 0;
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === 'http://localhost:4174') return route.continue();
      if (url.hostname === 'identitytoolkit.googleapis.com' && url.pathname === '/v1/projects') {
        projectRequests++;
        return route.fulfill({
          status: scenario.backendMessage === null ? 200 : 400,
          contentType: 'application/json',
          body: JSON.stringify(scenario.backendMessage === null ? { authorizedDomains: [] } : {
            error: { message: scenario.backendMessage },
            email: 'private-account@example.test',
            oauthAccessToken: privateToken,
            oauthIdToken: privateToken,
            oauthPendingToken: privateUrl,
          }),
        });
      }
      // Tests must never contact Google, a live Firebase project, or another origin.
      return route.abort();
    });
    await page.goto('/');
    const panel = page.locator('.sandbox-panel');
    await panel.locator('summary').click();
    const signIn = panel.getByRole('button', { name: 'Sign in with Google', exact: true });
    await expect(signIn).toBeEnabled();
    await signIn.click();
    const alert = panel.getByRole('alert');
    await expect(alert).toContainText(`code=${scenario.code}`);
    await expect(alert).toContainText('name=FirebaseError');
    await expect(alert).toContainText('isError=true');
    await expect(alert).toContainText('own enumerable properties=[code, customData, name]');
    await expect(alert).not.toContainText('[redacted]');
    await expect(alert).not.toContainText(privateUrl);
    await expect(alert).not.toContainText(privateToken);
    await expect(alert).not.toContainText('private-account@example.test');
    await expect(alert).not.toContainText('oauthAccessToken');
    expect(projectRequests).toBe(1);
    await expect(signIn).toBeEnabled();
    await expect(page.getByRole('button', { name: '＋ Log Something' })).toBeEnabled();
  });
}
