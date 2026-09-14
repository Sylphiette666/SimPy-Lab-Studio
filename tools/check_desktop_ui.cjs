/* Attach to an EXE started with --debug-port 9227 and an isolated --data-dir. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.connectOverCDP(process.env.STUDIO_CDP || 'http://127.0.0.1:9227');
  try {
    const page = browser.contexts()[0].pages().find(p => p.url().startsWith('http://127.0.0.1:'));
    assert.ok(page, 'Packaged native WebView2 window must load the local app');
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    assert.equal(await page.locator('#global-error').isVisible(), false);
    await page.locator('#until-days').fill('2');
    await page.locator('#warmup-days').fill('0.1');
    await page.locator('#replications').fill('3');
    await page.locator('#apply-model').click();
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled);
    await page.locator('#restart-playback').click();
    await page.locator('#play-pause').click();
    await page.waitForTimeout(1200);
    assert.ok(Number(await page.locator('#seek').inputValue()) > 0, 'Replay moves in native window');
    await page.locator('#play-pause').click();
    await page.locator('#seek').evaluate(el => {el.value=Math.round(Number(el.max)*.7);el.dispatchEvent(new Event('input',{bubbles:true}));});
    await page.locator('#run-study').click();
    await page.locator('#study-summary').waitFor({state:'visible',timeout:60000});
    assert.match(await page.locator('#study-summary').textContent(), /3 次独立重复/);
    assert.ok(await page.locator('#version-rows .ci').count() >= 3);
    await page.locator('#ai-settings').click();
    await page.locator('#new-ai-profile').click();
    await page.locator('#ai-service-preset').selectOption('deepseek');
    await page.locator('#save-ai-settings').click();
    await page.waitForFunction(() => !document.querySelector('#settings-dialog').open);
    assert.match(await page.locator('#ai-hint').textContent(), /密钥/);
    await page.locator('#ai-profile-select').selectOption('default');
    await page.waitForFunction(() => document.querySelector('#ai-profile-select').value === 'default' && !document.querySelector('#ai-profile-select').disabled);
    const imageRoot = path.resolve(__dirname, '../docs/images');
    fs.mkdirSync(imageRoot, {recursive:true});
    await page.waitForTimeout(3800);
    await page.screenshot({path:path.join(imageRoot, 'desktop-studio.png')});
    await page.locator('#ai-settings').click();
    await page.screenshot({path:path.join(imageRoot, 'desktop-models.png')});
    await page.locator('#settings-dialog .close-dialog').first().click();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ok:true, nativeWindow:true, checks:['real preview', 'play/pause/seek', 'study', 'model profiles', 'screenshots'], url:page.url()}));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;});
