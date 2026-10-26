import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

// HTTPS is opt-in for a trusted, local LAN certificate. No proxy or API routes.
const cert = process.env.PWA_TLS_CERT;
const key = process.env.PWA_TLS_KEY;
if (Boolean(cert) !== Boolean(key)) throw new Error('Set both PWA_TLS_CERT and PWA_TLS_KEY.');
const https = cert && key ? { cert: readFileSync(cert), key: readFileSync(key) } : undefined;

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  server: { host: '127.0.0.1', port: 5173, strictPort: true, https,
    fs: { strict: true, allow: [fileURLToPath(new URL('.', import.meta.url))] } },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true, https },
});
