/* Requires tests/studio_browser_fixture.py. Never calls a real model provider. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('Set STUDIO_TEST_URL to the isolated injected-agent fixture.');
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1050}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const fixture = await page.request.get(`${url}/fixture-health`);
    assert.equal(fixture.ok(), true);
    assert.equal((await fixture.json()).injected_test_agent, true);
    await page.goto(url, {waitUntil: 'networkidle'});
    await page.waitForFunction(() => document.querySelector('#model-name').value);
    if (await page.locator('#edit-model').count()) await page.locator('#edit-model').click();
    await page.locator('#until-days').fill('0.05');
    await page.locator('#warmup-days').fill('0.005');
    await page.locator('#replications').fill('2');
    await page.locator('#apply-model').click();
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled);
    await page.locator('#seek').evaluate(el => {el.value = Math.round(Number(el.max) * .6); el.dispatchEvent(new Event('input', {bubbles:true}));});
    const clock = await page.locator('#line-clock').textContent();
    const suffix = Date.now();
    const names = [`浏览器模型 A ${suffix}`, `浏览器模型 B ${suffix}`];
    const ids = [];
    for (let index = 0; index < 2; index++) {
      await page.locator('#ai-settings').click();
      await page.locator('#new-ai-profile').click();
      await page.locator('#ai-profile-name').fill(names[index]);
      await page.locator('#ai-model').fill(`fixture-model-${index}`);
      await page.locator('#ai-base-url').fill(`https://fixture-${index}.example.invalid/v1`);
      await page.locator('#ai-format').selectOption('chat_completions');
      await page.locator('#ai-api-key').fill(`fixture-only-key-${index}`);
      await page.locator('#save-ai-settings').click();
      await page.waitForFunction(() => !document.querySelector('#settings-dialog').open);
      ids.push(await page.locator('#ai-profile-select').inputValue());
    }
    assert.notEqual(ids[0], ids[1]);
    assert.equal(await page.locator('#line-clock').textContent(), clock);
    await page.locator('#ai-profile-select').selectOption(ids[0]);
    await page.waitForFunction(model => document.querySelector('#ai-hint').textContent.includes(model), 'fixture-model-0');
    await page.locator('#prompt').fill('将第一台设备可用率提高到90%');
    const adjusted = page.waitForResponse(r => r.url().endsWith('/adjust') && r.request().method() === 'POST');
    await page.locator('#send-prompt').click();
    const adjustmentResponse = await adjusted;
    assert.equal(adjustmentResponse.ok(), true, await adjustmentResponse.text());
    const session = await adjustmentResponse.json();
    assert.equal(session.versions.at(-1).ai_config.model, 'fixture-model-0');
    assert.equal(session.messages.at(-1).ai_config.model, 'fixture-model-0');
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    await page.locator('#ai-profile-select').selectOption(ids[1]);
    await page.waitForFunction(model => document.querySelector('#ai-hint').textContent.includes(model), 'fixture-model-1');
    assert.match(await page.locator('#messages').textContent(), /fixture-model-0/);
    await page.reload({waitUntil: 'networkidle'});
    await page.waitForFunction(id => document.querySelector('#ai-profile-select')?.value === id, ids[1]);
    await page.locator('#ai-settings').click();
    assert.equal(await page.locator('#ai-api-key').inputValue(), '');
    assert.equal(await page.locator('#ai-model').inputValue(), 'fixture-model-1');
    const output = path.resolve(__dirname, '../outputs/studio_qa');
    fs.mkdirSync(output, {recursive: true});
    await page.screenshot({path: path.join(output, 'model-profiles-desktop.png')});
    await page.setViewportSize({width:390, height:844});
    await page.screenshot({path: path.join(output, 'model-profiles-mobile.png'), fullPage:true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    const catalog = await page.request.get(`${url}/api/studio/ai/profiles`);
    assert.equal((await catalog.text()).includes('fixture-only-key-'), false);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'model-profiles-check.json'), JSON.stringify({
      ok:true, fixture:'injected agent; no external LLM calls', checks:['create two connections',
        'save and activate', 'quick switch', 'replay state preserved', 'correct model reaches agent',
        'historical model attribution', 'reload persistence', 'keys not echoed', 'responsive modal'],
    }, null, 2));
    console.log('Model profile browser checks passed');
  } finally { await browser.close(); }
})().catch(e => {console.error(e);process.exitCode=1;});
