/* Uses only the isolated injected-agent fixture. Never sends a real AI request. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('STUDIO_TEST_URL must point to the isolated fixture.');
  const output = path.resolve(__dirname, '../outputs/credential-storage-qa');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const checks = [], errors = [];
  let aiCalls = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/\/adjust(?:ments)?$/.test(request.url()) && request.method()==='POST') aiCalls++; });
  const secret = 'fixture-browser-encrypted-key-only';
  const catalog = async () => {
    const response = await page.request.get(url + '/api/studio/ai/profiles');
    const raw = await response.text();
    assert.equal(raw.includes(secret), false);
    assert.equal(raw.includes('protected_keys'), false);
    return response.json();
  };
  const open = async () => {
    await page.locator('#ai-settings').click();
    await page.waitForFunction(() => document.querySelector('#settings-dialog').open &&
      !document.querySelector('#save-ai-settings').disabled);
  };
  const close = () => page.locator('#settings-dialog .close-dialog').first().click();
  async function save() {
    await page.locator('#save-ai-settings').click();
    await page.waitForFunction(() => !document.querySelector('#settings-dialog').open);
  }
  try {
    const fixture = await (await page.request.get(url + '/fixture-health')).json();
    assert.equal(fixture.injected_test_agent, true);
    assert.equal((await catalog()).key_storage_available, true, 'Run the encryption UI checks on Windows.');
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    await open();
    await page.locator('#new-ai-profile').click();
    assert.equal(await page.locator('#ai-remember-key').isChecked(), false);
    await page.locator('#ai-profile-name').fill('加密保存测试 ' + Date.now());
    await page.locator('#ai-model').fill('fixture-key-model');
    await page.locator('#ai-base-url').fill('https://credential-fixture.invalid/v1');
    await page.locator('#ai-api-key').fill(secret);
    await page.locator('#ai-remember-key').check();
    await save();
    const id = (await catalog()).active_profile_id;
    const profile = async () => (await catalog()).profiles.find(item => item.id === id);
    assert.equal((await profile()).remember_key, true);
    assert.equal((await profile()).has_key, true);
    await page.reload({ waitUntil: 'networkidle' });
    await open();
    await page.waitForFunction(value => document.querySelector('#ai-api-key').value === value, secret);
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'password');
    await page.locator('#toggle-ai-key').click();
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'text');
    await page.locator('#toggle-ai-key').click();
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'password');
    assert.equal(await page.locator('#ai-remember-key').isChecked(), true);
    assert.match(await page.locator('#ai-key-state').textContent(), /已在本机加密保存/);
    await page.locator('#ai-remember-key').uncheck();
    await close();
    await open();
    assert.equal(await page.locator('#ai-remember-key').isChecked(), true, 'Cancel must not remove the saved key.');
    checks.push('Saved key autofills masked; show/hide works; cancel keeps persisted settings.');
    await page.locator('#copy-ai-profile').click();
    assert.equal(await page.locator('#ai-api-key').inputValue(), '');
    assert.equal(await page.locator('#ai-remember-key').isChecked(), false);
    await save();
    const copyId = (await catalog()).active_profile_id;
    assert.equal((await catalog()).profiles.find(item => item.id === copyId).has_key, false);
    await open();
    await page.locator('#ai-profile-edit-select').selectOption(id);
    assert.equal(await page.locator('#ai-remember-key').isChecked(), true);
    await page.locator('#ai-remember-key').uncheck();
    await save();
    assert.equal((await profile()).remember_key, false);
    assert.equal((await profile()).has_key, true);
    await open();
    await page.locator('#ai-remember-key').check();
    await save();
    assert.equal((await profile()).remember_key, true, 'An existing in-memory key can be saved without resubmitting it.');
    await open();
    await page.locator('#ai-clear-key').check();
    assert.equal(await page.locator('#ai-api-key').isDisabled(), true);
    assert.equal(await page.locator('#ai-remember-key').isDisabled(), true);
    await save();
    assert.equal((await profile()).has_key, false);
    assert.equal((await profile()).remember_key, false);
    checks.push('Copy isolates credentials; opting out retains only memory; opting back in and complete removal work.');
    await open();
    await page.locator('#ai-api-key').fill(secret);
    await page.locator('#ai-remember-key').check();
    await save();
    await open();
    await page.locator('#ai-base-url').fill('https://changed-credential-fixture.invalid/v1');
    await save();
    assert.equal((await profile()).has_key, false);
    assert.equal((await profile()).remember_key, false);
    await open();
    await page.locator('#ai-remember-key').check();
    await page.locator('#save-ai-settings').click();
    await page.locator('#settings-error').waitFor({ state: 'visible' });
    assert.match(await page.locator('#settings-error').textContent(), /填写有效密钥/);
    assert.equal((await profile()).remember_key, false);
    await page.locator('#ai-api-key').fill(secret);
    await save();
    await open();
    for (const size of [{ width: 1440, height: 980 }, { width: 960, height: 680 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(size);
      await page.locator('#ai-remember-key').scrollIntoViewIfNeeded();
      const layout = await page.locator('#settings-dialog').evaluate(element => {
        const box = element.getBoundingClientRect();
        return { left: box.left, right: box.right, top: box.top, bottom: box.bottom,
          overflow: element.scrollWidth > element.clientWidth + 1 };
      });
      assert.ok(layout.left >= 0 && layout.right <= size.width && layout.top >= 0 && layout.bottom <= size.height && !layout.overflow);
      await page.screenshot({ path: path.join(output, `key-settings-${size.width}.png`) });
      await page.locator('#save-ai-settings').scrollIntoViewIfNeeded();
      assert.equal(await page.locator('#save-ai-settings').isVisible(), true);
    }
    checks.push('Endpoint change clears the old key; missing-key opt-in fails clearly; controls fit all window sizes.');
    await close();
    assert.equal(aiCalls, 0);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser-checks.json'), JSON.stringify({ ok: true, checks, errors, aiCalls }, null, 2));
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
