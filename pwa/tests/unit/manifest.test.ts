import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';
import manifest from '../../public/manifest.webmanifest?raw';
import { contract } from '../../src/model';

it('supplies a standalone manifest with local branded PNG icons and desktop colors', () => {
  const data = JSON.parse(manifest);
  expect(data.name).toBe('Zymptom');
  expect(data.display).toBe('standalone');
  expect(data.start_url).toBe('/');
  expect(data.scope).toBe('/');
  expect(data.theme_color).toBe(contract.palette.BACKGROUND);
  expect(data.background_color).toBe(contract.palette.BACKGROUND);
  expect(data.icons.map((icon: { sizes: string }) => icon.sizes)).toContain('192x192');
  expect(data.icons.map((icon: { sizes: string }) => icon.sizes)).toContain('512x512');
  for (const icon of data.icons) {
    const file = readFileSync(new URL(`../../public${icon.src}`, import.meta.url));
    expect(file.subarray(1, 4).toString()).toBe('PNG');
    expect(`${file.readUInt32BE(16)}x${file.readUInt32BE(20)}`).toBe(icon.sizes);
  }
  const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
  expect(html).toContain('width=device-width');
  expect(html).not.toContain('user-scalable=no');
  expect(html).toContain('apple-touch-icon');
});
