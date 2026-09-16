/* Optional browser smoke test. Requires Playwright and a running local Studio.
   STUDIO_URL=http://127.0.0.1:8765; screenshots and downloads go to outputs/studio_qa.
   This creates an isolated short experiment and never calls a paid LLM endpoint. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'outputs', 'studio_qa');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const url = process.env.STUDIO_URL || 'http://127.0.0.1:8765';
  try {
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    await page.locator('#new-session').click();
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    assert.equal(await page.locator('#production-line').count(), 1);
    if (await page.locator('#edit-model').count()) await page.locator('#edit-model').click();
    await page.locator('#until-days').fill('0.05');
    await page.locator('#warmup-days').fill('0.005');
    await page.locator('#replications').fill('2');
    const saved = page.waitForResponse(r => r.url().endsWith('/versions') && r.request().method() === 'POST');
    await page.locator('#apply-model').click();
    const savedResponse = await saved;
    assert.equal(savedResponse.status(), 200, await savedResponse.text());
    const session = await savedResponse.json();
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled, null, { timeout: 60000 });
    await page.locator('#seek').evaluate(el => {
      el.value = String(Math.floor(Number(el.max) * 0.8));
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const beforePlay = Number(await page.locator('#seek').inputValue());
    await page.locator('#play-pause').click();
    await page.waitForFunction(index => Number(document.querySelector('#seek').value) > index, beforePlay);
    await page.locator('#play-pause').click();
    assert.equal(await page.locator('#play-pause').getAttribute('aria-label'), '播放仿真回放');
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: path.join(output, 'studio-desktop.png'), fullPage: true });
    assert.notEqual((await page.locator('#metric-throughput').textContent()).trim(), '—');

    if (await page.locator('#nav-results').count()) await page.locator('#nav-results').click();
    await page.locator('#run-study').click();
    await page.waitForFunction(() => !document.querySelector('#study-summary').hidden, null, { timeout: 60000 });
    assert.match(await page.locator('#version-rows').textContent(), /\d/);
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: path.join(output, 'studio-results.png'), fullPage: true });
    if (await page.locator('#close-results').count()) await page.locator('#close-results').click();

    const downloadReady = page.waitForEvent('download');
    await page.locator('#export-session').click();
    const download = await downloadReady;
    await download.saveAs(path.join(output, download.suggestedFilename()));
    assert.match(download.suggestedFilename(), /\.zip$/);
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    assert.equal(await page.locator('#until-days').inputValue(), '0.05');
    await page.locator('#ai-settings').click();
    await page.waitForFunction(() => !document.querySelector('#save-ai-settings').disabled);
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'password');
    await page.keyboard.press('Escape');

    if (await page.locator('#nav-results').count()) await page.locator('#nav-results').click();
    const restoring = page.waitForResponse(r => r.url().endsWith('/restore') && r.request().method() === 'POST');
    await page.locator('.version-restore').last().click();
    const restored = await (await restoring).json();
    if (await page.locator('#results-dialog[open]').count()) await page.locator('#close-results').click();
    assert.equal(restored.versions.length, 3);
    assert.equal(await page.locator('#until-days').inputValue(), '30');
    const importing = page.waitForResponse(r => r.url().endsWith('/versions') && r.request().method() === 'POST');
    const customConfig = structuredClone(session.versions.at(-1).config);
    customConfig.buffers[0].capacity = 8;
    await page.locator('#config-file').setInputFiles({name: 'custom-model.json', mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({mode: 'custom', config: customConfig}))});
    assert.equal((await importing).status(), 200);
    await page.waitForFunction(() => document.querySelector('#model-mode').value === 'custom');
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled, null, {timeout: 60000});
    await page.locator('#seek').evaluate(el => { el.value = el.max; el.dispatchEvent(new Event('input', {bubbles: true})); });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'studio-mobile.png'), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser-check.json'), JSON.stringify({
      ok: true, session_id: session.id, checks: ['model edit', 'actual simulation preview',
        'seek and indicators', 'replicated study', 'ZIP export', 'session reload',
        'empty browser key', 'version restore', 'custom configuration import',
        'play and pause', 'responsive layout', 'no page exceptions'], errors,
    }, null, 2));
    console.log(JSON.stringify({ ok: true, output, session_id: session.id }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
