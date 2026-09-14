import { chromium } from 'playwright';
const browser = await chromium.launch({ channel: 'chromium', headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
await page.goto('http://127.0.0.1:8321/');
await page.waitForSelector('[data-testid=feature-profile]', { timeout: 20000 });
await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('features'), null, { timeout: 20000 });
await page.waitForTimeout(1200);
await page.click('[data-testid=feature-slot_cut]');
await page.waitForTimeout(500);
await page.screenshot({ path: 'screenshot.png' });
const px = await page.evaluate(() => {
  const c = document.querySelector('canvas'); const gl = c.getContext('webgl2') || c.getContext('webgl');
  const buf = new Uint8Array(4); gl.readPixels(Math.floor(c.width * 0.5), Math.floor(c.height * 0.38), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, buf); return Array.from(buf);
});
console.log('served bundle: features rendered, centre-ish pixel', px, 'errors:', errors.length ? errors : 'none');
// sketch mode screenshot with the corrected grid
await page.click('[data-testid=feature-holes]');
await page.click('.btn:has-text("edit sketch")');
await page.waitForSelector('[data-testid=sketch-bar]');
await page.waitForTimeout(600);
await page.screenshot({ path: 'shot-sketch.png' });
await browser.close();
