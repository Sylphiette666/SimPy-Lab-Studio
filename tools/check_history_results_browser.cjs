/* Historical result browsing against an isolated injected-agent fixture.
 * STUDIO_TEST_URL must point to tests/studio_browser_fixture.py. No real AI calls.
 */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('Set STUDIO_TEST_URL to the isolated fixture.');
  const output = path.resolve(__dirname, '../outputs/history-results-qa');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 980 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [], writes = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.method() === 'POST') writes.push(request.url()); });
  async function api(endpoint, data) {
    const response = data === undefined ? await context.request.get(url + endpoint)
      : await context.request.post(url + endpoint, { data });
    assert.ok(response.ok(), `${endpoint}: ${response.status()} ${await response.text()}`);
    return response.json();
  }
  try {
    assert.equal((await api('/fixture-health')).injected_test_agent, true, 'Never test against the user service.');
    const config = (await api('/api/studio/bootstrap')).template;
    Object.assign(config, { until_seconds: 7200, warmup_seconds: 600, replications: 3 });
    let session = await api('/api/studio/sessions', { config, mode: 'paper' });
    const sessionPath = `/api/studio/sessions/${session.id}`;
    const baseline = session.active_version_id;
    async function run(versionId, kind = 'study') {
      let result = await api(sessionPath + '/runs', { version_id: versionId, kind });
      for (let attempt = 0; attempt < 120 && !['succeeded', 'failed'].includes(result.status); attempt++) {
        await new Promise(resolve => setTimeout(resolve, 150));
        result = await api(sessionPath + '/runs/' + result.id);
      }
      assert.equal(result.status, 'succeeded', JSON.stringify(result.error));
      return result;
    }
    const studies = new Map([[baseline, await run(baseline)]]);
    const adjusted = await api(sessionPath + '/adjust', { expected_version_id: baseline, prompt: '第一台设备可用率提高到90%' });
    session = adjusted;
    assert.ok(session, 'The injected adjustment must return a session.');
    const historical = session.active_version_id;
    studies.set(historical, await run(historical));
    const nextConfig = structuredClone(session.versions.at(-1).config);
    nextConfig.machines[0].cycle_time_seconds = 180;
    session = await api(sessionPath + '/versions', { config: nextConfig, mode: 'paper', expected_version_id: historical, label: '尚未评估的历史模型' });
    const untested = session.active_version_id;
    nextConfig.machines[1].cycle_time_seconds = 150;
    session = await api(sessionPath + '/versions', { config: nextConfig, mode: 'paper', expected_version_id: untested, label: '当前运行方案' });
    const active = session.active_version_id;
    studies.set(active, await run(active));
    await run(active, 'preview');
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    await page.evaluate(id => localStorage.setItem('simlab.studio.session', id), session.id);
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => !document.querySelector('#play-pause').disabled);
    const activeLabel = await page.locator('#active-label').textContent();
    const machineInput = await page.locator('#machine-0-cycle_time_seconds').inputValue();
    const metricKeys = ['throughput_per_hour', 'avg_wip', 'specific_energy_kwh_per_part'];
    async function assertVersion(id) {
      assert.equal(await page.locator('#results-dialog').getAttribute('data-version-id'), id);
      const actual = await page.locator('#result-cards strong').allTextContents();
      const expected = studies.get(id).result.summary;
      metricKeys.forEach((metric, index) => assert.ok(Math.abs(Number(actual[index].replaceAll(',', '')) - expected.find(row => row.metric === metric).mean) < .0051));
      assert.equal(await page.locator('#active-label').textContent(), activeLabel);
      assert.equal(await page.locator('#machine-0-cycle_time_seconds').inputValue(), machineInput);
    }
    await page.locator('#nav-results').click();
    await assertVersion(active);
    const activeCards = await page.locator('#result-cards strong').allTextContents();
    const writesBefore = writes.length;
    await page.locator(`#version-rows tr[data-version-id="${historical}"]`).getByRole('button', { name: '详情', exact: true }).click();
    await assertVersion(historical);
    assert.notDeepEqual(await page.locator('#result-cards strong').allTextContents(), activeCards);
    assert.equal(await page.locator('#info-dialog').evaluate(element => element.open), false);
    assert.match(await page.locator('#result-evaluation').textContent(), /浏览器自动化测试响应/);
    assert.match(await page.locator('#result-evaluation').textContent(), /对照方案：V0/);
    assert.match(await page.locator('#result-version-details').textContent(), /使用的 AI 模型/);
    assert.equal(await page.locator('#run-study').isVisible(), false);
    assert.equal(await page.locator('#results-dialog').evaluate(element => element.scrollTop), 0);
    const models = page.locator('#result-version-details details').last();
    await models.locator('summary').click();
    assert.deepEqual(JSON.parse(await models.locator('pre').textContent()), session.versions.find(item => item.id === historical).config);
    // An unrelated evaluation finishing must not swap the version being read.
    await page.evaluate(() => renderVersions());
    await assertVersion(historical);
    assert.equal(await models.evaluate(element => element.open), true);
    await models.locator('summary').click();
    const ready = page.waitForEvent('download');
    await page.locator('#download-report').click();
    const download = await ready;
    assert.ok(download.suggestedFilename().includes(historical.slice(0, 8)));
    const reportPath = path.join(output, download.suggestedFilename());
    await download.saveAs(reportPath);
    const report = fs.readFileSync(reportPath, 'utf8');
    assert.match(report, /浏览器自动化测试响应/);
    assert.equal(report.includes('当前运行方案'), false);
    checks.push('Historical details show the selected real study, confidence intervals, explanation, changes, AI source and model; export matches that version.');
    await page.locator('#result-version-select').selectOption(untested);
    assert.match(await page.locator('#result-status').textContent(), /尚未完成完整评估/);
    assert.deepEqual(await page.locator('#result-cards strong').allTextContents(), ['—', '—', '—']);
    assert.equal(await page.locator('#download-report').isDisabled(), true);
    assert.equal(await page.locator('#study-summary').isVisible(), false);
    await page.locator('#result-version-select').selectOption(baseline);
    await assertVersion(baseline);
    assert.match(await page.locator('#result-evaluation').textContent(), /初始输入模型/);
    assert.equal((await page.locator('#result-evaluation').textContent()).includes('对照方案：'), false);
    assert.equal((await page.locator('#result-version-details').textContent()).includes('使用的 AI 模型'), false);
    await page.locator('#return-current-results').click();
    await assertVersion(active);
    assert.equal(await page.locator('#run-study').isVisible(), true);
    assert.equal(writes.length, writesBefore, 'Reading and exporting must not enqueue runs or restore versions.');
    checks.push('Unassessed versions have no borrowed metrics; initial version and return-to-current work without mutations.');
    await page.locator('#close-results').click();
    await page.locator('#play-pause').click();
    await page.locator('#nav-results').click();
    await page.locator('#result-version-select').selectOption(historical);
    const start = Number(await page.locator('#seek').inputValue());
    await page.waitForFunction(before => Number(document.querySelector('#seek').value) > before, start);
    assert.equal(await page.locator('#play-pause').getAttribute('aria-label'), '暂停仿真回放');
    await page.mouse.click(4, 480);
    assert.equal(await page.locator('#results-dialog').evaluate(element => element.open), false);
    await page.locator('#nav-results').click();
    await assertVersion(active);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#results-dialog').evaluate(element => element.open), false);
    await page.locator('#play-pause').click();
    checks.push('Reading history does not pause replay; backdrop and Escape close the panel; reopening evaluation returns to current.');
    await page.locator('#nav-results').click();
    await page.locator('#result-version-select').selectOption(historical);
    for (const size of [{ width: 1440, height: 980 }, { width: 960, height: 680 }, { width: 390, height: 740 }]) {
      await page.setViewportSize(size);
      await page.locator('#results-dialog').evaluate(element => element.scrollTop = 0);
      const layout = await page.locator('#results-dialog').evaluate(element => {
        const box = element.getBoundingClientRect();
        return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, overflow: element.scrollWidth > element.clientWidth + 1,
          outside: [...element.querySelectorAll('*')].filter(child => child.getBoundingClientRect().right > box.right).map(child => ({tag: child.tagName, id: child.id, className: child.className, right: child.getBoundingClientRect().right})).slice(0, 12) };
      });
      assert.ok(layout.left >= 0 && layout.right <= size.width && layout.top >= 0 && layout.bottom <= size.height && !layout.overflow, JSON.stringify(layout));
      await page.screenshot({ path: path.join(output, `history-${size.width}.png`) });
    }
    checks.push('Historical evaluation fits desktop, minimum desktop and narrow windows.');
    const saved = await api(sessionPath);
    assert.equal(saved.active_version_id, active);
    assert.equal(saved.versions.length, session.versions.length);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser-checks.json'), JSON.stringify({ ok: true, checks, errors }, null, 2));
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
