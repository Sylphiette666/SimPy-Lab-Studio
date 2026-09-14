/* Run ONLY against an isolated Studio with an injected test agent, never a real API.
   Set STUDIO_TEST_URL to that test server. Creates no production credentials. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
(async () => {
  if (!process.env.STUDIO_TEST_URL) throw new Error('STUDIO_TEST_URL must point to an injected-agent test fixture');
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1050}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const fixture = await page.request.get(`${process.env.STUDIO_TEST_URL}/fixture-health`);
    assert.equal(fixture.ok(), true, 'This script requires the isolated injected-agent fixture server');
    assert.equal((await fixture.json()).injected_test_agent, true);
    await page.goto(process.env.STUDIO_TEST_URL, {waitUntil: 'networkidle'});
    await page.waitForFunction(() => document.querySelector('#model-name').value);
    await page.locator('#until-days').fill('0.05');
    await page.locator('#warmup-days').fill('0.005');
    await page.locator('#replications').fill('2');
    await page.locator('#apply-model').click();
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled);
    await page.locator('#prompt').fill('将第一台设备可用率提高到90%，保持其他设置不变');
    const response = page.waitForResponse(r => r.url().endsWith('/adjust') && r.request().method() === 'POST');
    await page.locator('#send-prompt').click();
    const adjustedResponse = await response;
    assert.equal(adjustedResponse.status(), 200, await adjustedResponse.text());
    const session = await adjustedResponse.json();
    const version = session.versions.at(-1);
    assert.equal(version.source, 'llm');
    assert.equal(version.config.machines[0].availability, 0.9);
    assert.equal(version.config.until_seconds, 4320);
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled);
    assert.match(await page.locator('#messages').textContent(), /自动化测试响应/);
    await page.locator('#seek').evaluate(el => {el.value = el.max; el.dispatchEvent(new Event('input', {bubbles:true}));});
    const loaded = await (await page.request.get(`${process.env.STUDIO_TEST_URL}/api/studio/sessions/${session.id}`)).json();
    assert.equal(loaded.runs.filter(r => r.version_id === version.id && r.kind === 'preview').length, 1);
    assert.deepEqual(errors, []);
    const output = path.resolve(__dirname, '../outputs/studio_qa');
    fs.mkdirSync(output, {recursive: true});
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({path: path.join(output, 'studio-ai-test.png'), fullPage: true});
    fs.writeFileSync(path.join(output, 'ai-browser-check.json'), JSON.stringify({
      ok:true, fixture:'injected test agent; no external LLM request', session_id:session.id,
      checks:['natural-language submit', 'validated parameter application', 'new LLM version',
        'one automatic actual simulation preview', 'conversation history', 'no page exceptions'],
    }, null, 2));
    console.log('Injected-agent browser flow passed');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
