/* Isolated fixture only. Fake keys; no provider requests. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('STUDIO_TEST_URL must point to the isolated fixture.');
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true});
  const page = await browser.newPage();
  const secret = 'fixture-deepseek-settings-key';
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'PUT' && /\/ai\/profiles\//.test(request.url())) requests.push(request.postDataJSON());
    if (/\/adjust(?:ments)?$/.test(request.url())) throw new Error('No AI calls allowed.');
  });
  const open = async () => {
    await page.locator('#ai-settings').click();
    await page.waitForFunction(() => document.querySelector('#settings-dialog').open && !document.querySelector('#save-ai-settings').disabled);
  };
  const close = async () => {
    await page.locator('#settings-dialog .close-dialog').first().click();
    await page.waitForFunction(() => !document.querySelector('#settings-dialog').open && !document.querySelector('#ai-api-key').value);
  };
  const waitKey = value => page.waitForFunction(value => document.querySelector('#ai-api-key').value === value, value);
  const save = async () => {
    await page.locator('#save-ai-settings').click();
    await page.waitForFunction(() => !document.querySelector('#settings-dialog').open);
  };
  try {
    assert.equal((await (await page.request.get(url + '/fixture-health')).json()).injected_test_agent, true);
    await page.goto(url, {waitUntil: 'networkidle'});
    await open();
    await page.locator('#new-ai-profile').click();
    await page.locator('#ai-service-preset').selectOption('deepseek-v4-flash');
    assert.equal(await page.locator('#ai-model').inputValue(), 'deepseek-v4-flash');
    assert.equal(await page.locator('#ai-base-url').inputValue(), 'https://api.deepseek.com/v1');
    assert.equal(await page.locator('#ai-format').inputValue(), 'chat_completions');
    await page.locator('#ai-api-key').fill(secret);
    await save();
    await open(); await waitKey(secret);
    const id = await page.locator('#ai-profile-edit-select').inputValue();
    await page.locator('#toggle-ai-key').click();
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'text');
    await close(); await open(); await waitKey(secret);
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'password');
    await page.locator('#ai-service-preset').selectOption('deepseek');
    assert.equal(await page.locator('#ai-model').inputValue(), 'deepseek-flash');
    assert.equal(await page.locator('#ai-api-key').inputValue(), secret, 'Same service preset retains the key.');
    await save();
    assert.equal('api_key' in requests.at(-1), false, 'Autofilled key is not sent as a replacement.');
    await open(); await waitKey(secret);
    await page.locator('#ai-api-key').fill('fixture-typed-replacement');
    await page.locator('#ai-profile-edit-select').selectOption('default');
    await page.locator('#ai-profile-edit-select').selectOption(id);
    assert.equal(await page.locator('#ai-api-key').inputValue(), 'fixture-typed-replacement');
    await close();

    // Deliberately hold the key response to cover edits while it is in flight.
    for (const scenario of ['typing', 'endpoint', 'copy', 'close']) {
      let release, capture;
      const gate = new Promise(resolve => { release = resolve; });
      const captured = new Promise(resolve => { capture = resolve; });
      await page.route('**/ai/profiles/*/key', async route => {
        const response = await route.fetch(); capture(); await gate;
        await route.fulfill({response});
      });
      await open(); await captured;
      if (scenario === 'typing') await page.locator('#ai-api-key').fill('fixture-race-new-key');
      if (scenario === 'endpoint') await page.locator('#ai-base-url').fill('https://different.invalid/v1');
      if (scenario === 'copy') await page.locator('#copy-ai-profile').click();
      if (scenario === 'close') await close();
      const returned = page.waitForResponse(response => /\/ai\/profiles\/[^/]+\/key$/.test(response.url()));
      release(); await returned;
      // Frame boundary lets the response's promise callbacks settle before asserting.
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.locator('#ai-api-key').inputValue(), scenario === 'typing' ? 'fixture-race-new-key' : '', scenario);
      await page.unroute('**/ai/profiles/*/key');
      if (scenario !== 'close') await close();
    }
    await open(); await waitKey(secret);
    await page.locator('#ai-base-url').fill('https://different.invalid/v1');
    assert.equal(await page.locator('#ai-api-key').inputValue(), '');
    await save();
    assert.equal('api_key' in requests.at(-1), false);
    const catalog = await (await page.request.get(url + '/api/studio/ai/profiles')).json();
    assert.equal(catalog.profiles.find(profile => profile.id === id).has_key, false);
    assert.equal(JSON.stringify(catalog).includes(secret), false);
    const stored = await page.evaluate(() => JSON.stringify({...localStorage}));
    assert.equal(stored.includes(secret), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ok: true, checks: ['DeepSeek presets', 'masked autofill and reopen', 'same-service preservation', 'draft isolation', 'late response guards', 'endpoint clearing', 'secret-free catalog and browser storage']}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
