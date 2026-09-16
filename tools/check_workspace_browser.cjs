/* Frontend workspace integration checks. Requires tests/studio_browser_fixture.py.
 * Set STUDIO_TEST_URL explicitly. This uses an injected agent and never contacts
 * a model provider. All test artifacts stay in ignored outputs/workspace_qa.
 */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('Set STUDIO_TEST_URL to the isolated injected-agent fixture.');
  const output = path.resolve(__dirname, '../outputs/workspace_qa');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 980 }, acceptDownloads: true,
  });
  const page = await context.newPage();
  const errors = [];
  const checks = [];
  const studies = [];
  const artifacts = [];
  let releaseAdjustment;
  let releasePreview;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && /\/sessions\/[^/]+\/runs$/.test(new URL(request.url()).pathname)) {
      const body = request.postDataJSON();
      if (body.kind === 'study') studies.push({ version_id: body.version_id, time: Date.now() });
    }
  });

  const paused = () => page.locator('#play-pause').getAttribute('aria-label');
  const frame = async () => Number(await page.locator('#seek').inputValue());
  const waitReady = () => page.waitForFunction(() => !document.querySelector('#play-pause').disabled,
    null, { timeout: 60000 });
  const waitPlaying = () => page.waitForFunction(() =>
    document.querySelector('#play-pause').getAttribute('aria-label') === '暂停仿真回放');
  async function ensurePaused() {
    if (await paused() === '暂停仿真回放') await page.locator('#play-pause').click();
    assert.equal(await paused(), '播放仿真回放');
  }
  async function seek(fraction) {
    await page.locator('#seek').evaluate((element, value) => {
      element.value = String(Math.floor(Number(element.max) * value));
      element.dispatchEvent(new Event('input', { bubbles: true }));
    }, fraction);
  }
  async function waitAdvance(start) {
    await page.waitForFunction(index => Number(document.querySelector('#seek').value) > index,
      start, { timeout: 5000 });
  }
  async function openModel() {
    if (!await page.locator('#model-dialog').evaluate(element => element.open)) {
      await page.locator('#edit-model').click();
    }
    assert.equal(await page.locator('#model-dialog').evaluate(element => element.open), true);
  }
  async function closeDialog(id) {
    const dialog = page.locator(`#${id}`);
    if (await dialog.evaluate(element => element.open)) {
      const dedicated = { 'model-dialog': '#close-model', 'results-dialog': '#close-results' }[id];
      await (dedicated ? page.locator(dedicated) : dialog.locator('.close-dialog').first()).click();
    }
  }
  async function results() {
    if (!await page.locator('#results-dialog').evaluate(element => element.open)) {
      if (await page.locator('#nav-results').isVisible()) await page.locator('#nav-results').click();
      else await page.locator('#view-final-results').click();
    }
    assert.equal(await page.locator('#results-dialog').evaluate(element => element.open), true);
  }
  async function screenshot(name) {
    await page.screenshot({ path: path.join(output, name), fullPage: true });
    artifacts.push(name);
  }
  async function download(selector, suffix) {
    const ready = page.waitForEvent('download');
    await page.locator(selector).click();
    const file = await ready;
    assert.equal(file.suggestedFilename().endsWith(suffix), true, file.suggestedFilename());
    const location = path.join(output, file.suggestedFilename());
    await file.saveAs(location);
    artifacts.push(file.suggestedFilename());
    return location;
  }
  async function layout(label) {
    const measure = await page.evaluate(() => {
      const bounds = selector => {
        const rect = document.querySelector(selector).getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height,
          right: rect.right, bottom: rect.bottom };
      };
      return { width: innerWidth, height: innerHeight,
        overflow: document.documentElement.scrollWidth > innerWidth + 2,
        stage: bounds('#line-stage'), dock: bounds('.workspace-dock'),
        pause: bounds('#play-pause'), prompt: bounds('#prompt'),
      };
    });
    checks.push({ name: `layout ${label}`, ...measure });
    assert.equal(measure.overflow, false, `${label}: document must not overflow horizontally`);
    assert.ok(measure.stage.width > measure.width * .53, `${label}: simulation must occupy most width`);
    assert.ok(measure.stage.height > measure.height * .35,
      `${label}: simulation must occupy a substantial share of the available height`);
    assert.ok(measure.pause.bottom <= measure.height + 2, `${label}: pause must remain in viewport`);
    assert.ok(measure.prompt.bottom <= measure.height + 2, `${label}: prompt must remain in viewport`);
    assert.ok(measure.dock.x >= measure.stage.right - 5, `${label}: metrics and assistant should stay beside simulation`);
  }

  try {
    const fixture = await page.request.get(`${url}/fixture-health`);
    assert.equal(fixture.ok(), true);
    assert.equal((await fixture.json()).injected_test_agent, true,
      'Refusing to exercise adjustment against a real provider.');
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    // The workspace now restores the last experiment across browser contexts.
    await page.locator('#new-session').click();
    await page.waitForFunction(() => !document.querySelector('#apply-model').disabled);
    assert.equal(await page.locator('#production-line').count(), 1);
    assert.equal(await page.locator('#model-dialog').evaluate(element => element.open), false);
    const sessionId = await page.evaluate(() => localStorage.getItem('simlab.studio.session'));
    assert.ok(sessionId);
    await screenshot('workspace-initial-qa.png');

    // Two deliberately non-routable connections exercise selection and attribution.
    const profiles = [];
    for (let index = 0; index < 2; index++) {
      await page.locator('#ai-settings').click();
      await page.locator('#new-ai-profile').click();
      await page.locator('#ai-profile-name').fill(`工作台测试 ${Date.now()}-${index}`);
      await page.locator('#ai-model').fill(`fixture-workspace-${index}`);
      await page.locator('#ai-base-url').fill(`https://fixture-workspace-${index}.example.invalid/v1`);
      await page.locator('#ai-format').selectOption('chat_completions');
      await page.locator('#ai-api-key').fill(`fixture-workspace-key-${index}`);
      await page.locator('#save-ai-settings').click();
      await page.waitForFunction(() => !document.querySelector('#settings-dialog').open);
      profiles.push(await page.locator('#ai-profile-select').inputValue());
    }
    assert.notEqual(profiles[0], profiles[1]);
    checks.push('API profiles retained');

    await openModel();
    await page.locator('#until-days').fill('0.05');
    await page.locator('#warmup-days').fill('0.005');
    await page.locator('#replications').fill('2');
    await page.locator('#replications').fill('0');
    await closeDialog('model-dialog');
    await page.locator('#run-preview').click();
    await page.waitForFunction(() => document.querySelector('#model-dialog').open);
    assert.equal(await page.locator('#replications').evaluate(element => element.validity.valid), false);
    assert.equal(await page.locator('#replications').isVisible(), true);
    await page.locator('#replications').fill('2');
    await page.locator('#warmup-days').fill('0.06');
    await closeDialog('model-dialog');
    await page.locator('#run-preview').click();
    await page.waitForFunction(() => document.querySelector('#model-dialog').open);
    assert.equal(await page.locator('#parameter-issues').isVisible(), true);
    assert.match(await page.locator('#parameter-issues').textContent(), /预热/);
    assert.match(await page.locator('#warmup-days').evaluate(element => element.validationMessage), /预热/);
    await page.locator('#warmup-days').fill('0.005');
    checks.push('invalid hidden model inputs reopen the editor with actionable validation');
    assert.equal(await page.locator('#buffer-0-capacity').isDisabled(), true);
    await page.locator('#model-mode').selectOption('custom');
    assert.equal(await page.locator('#buffer-0-capacity').isDisabled(), false);
    await page.locator('#model-mode').selectOption('paper');
    await page.locator('#edit-breaks').click();
    assert.equal(await page.locator('#breaks-dialog').evaluate(element => element.open), true);
    await closeDialog('breaks-dialog');
    const exportedModel = JSON.parse(fs.readFileSync(await download('#export-config', '.json'), 'utf8'));
    assert.equal(exportedModel.config.replications, 2);
    assert.equal(exportedModel.mode, 'paper');
    const saved = page.waitForResponse(response => response.url().endsWith('/versions') &&
      response.request().method() === 'POST');
    await page.locator('#apply-model').click();
    const savedResponse = await saved;
    assert.equal(savedResponse.status(), 200, await savedResponse.text());
    const editedSession = await savedResponse.json();
    await waitReady();
    await waitPlaying();
    assert.equal(await page.locator('#model-dialog').evaluate(element => element.open), false);
    assert.equal(await page.locator('body').evaluate(element => element.classList.contains('immersive')), true);
    assert.equal(await page.evaluate(() => document.fullscreenEnabled), true,
      'the desktop browser test must exercise the supported Fullscreen API path');
    await page.waitForFunction(() => Boolean(document.fullscreenElement));
    checks.push('edit input model and enter immersive preview');

    await page.locator('#playback-speed').selectOption('0.5');
    await seek(.35);
    const earlyMetrics = await page.locator('#metric-throughput').textContent();
    const earlyClock = await page.locator('#line-clock').textContent();
    await seek(.70);
    const lateMetrics = await page.locator('#metric-throughput').textContent();
    assert.notEqual(lateMetrics.trim(), '—');
    assert.notEqual(earlyMetrics, lateMetrics, 'sampled production metrics should change with playback position');
    assert.notEqual(earlyClock, await page.locator('#line-clock').textContent());
    const pausedIndex = await frame();
    await page.waitForTimeout(400);
    assert.equal(await frame(), pausedIndex, 'pause must stop advancement');
    await layout('1440x980 immersive');
    await screenshot('workspace-running-desktop-qa.png');
    checks.push('actual sampled metrics, seeking, and pause');

    // A second preview must not expose controls for stale frames while it computes.
    const previewGate = new Promise(resolve => { releasePreview = resolve; });
    let notifyPreviewPoll;
    const previewPolled = new Promise(resolve => { notifyPreviewPoll = resolve; });
    const previewPollPattern = '**/api/studio/sessions/*/runs/*';
    const holdPreviewPoll = async route => {
      notifyPreviewPoll();
      await previewGate;
      await route.continue();
    };
    await page.route(previewPollPattern, holdPreviewPoll);
    const secondPreview = page.waitForResponse(response => response.url().endsWith('/runs') &&
      response.request().method() === 'POST' && response.request().postDataJSON().kind === 'preview');
    await page.locator('#run-preview').click();
    assert.equal((await secondPreview).ok(), true);
    await previewPolled;
    for (const selector of ['#play-pause', '#restart-playback', '#seek']) {
      assert.equal(await page.locator(selector).isDisabled(), true,
        `${selector}: old replay transport must wait for the new preview`);
    }
    const staleFrame = await frame();
    await page.locator('#line-stage').click({ position: { x: 10, y: 10 } });
    await page.keyboard.press('Space');
    await page.waitForTimeout(350);
    assert.equal(await frame(), staleFrame);
    releasePreview();
    await waitReady();
    await waitPlaying();
    await page.unroute(previewPollPattern, holdPreviewPoll);
    checks.push('rerunning preview locks stale transport until the new frames arrive');

    // Typing, suggestions, and changing the selected model cannot interrupt replay.
    await seek(.2);
    await page.locator('#play-pause').click();
    let before = await frame();
    await page.locator('#prompt').fill('将第一台设备可用率提高到90%，保留其余参数');
    await waitAdvance(before);
    assert.equal(await paused(), '暂停仿真回放');
    before = await frame();
    await page.locator('.prompt-chip').first().click();
    await waitAdvance(before);
    assert.equal(await paused(), '暂停仿真回放');
    before = await frame();
    await page.locator('#ai-profile-select').selectOption(profiles[0]);
    await page.waitForFunction(() => document.querySelector('#ai-hint').textContent.includes('fixture-workspace-0'));
    await waitAdvance(before);
    assert.equal(await paused(), '暂停仿真回放');
    checks.push('typing, suggestions, and model selection do not pause replay');

    // Hold the request before the fixture sees it, so waiting behavior is observable.
    const adjustmentGate = new Promise(resolve => { releaseAdjustment = resolve; });
    let notifyIntercept;
    const intercepted = new Promise(resolve => { notifyIntercept = resolve; });
    const adjustmentPattern = '**/api/studio/sessions/*/adjustments';
    const holdAdjustment = async route => {
      notifyIntercept(route.request().postDataJSON());
      await adjustmentGate;
      await route.continue();
    };
    await page.route(adjustmentPattern, holdAdjustment);
    const adjustmentReady = page.waitForResponse(response => response.url().endsWith('/adjustments') &&
      response.request().method() === 'POST');
    await page.locator('#prompt').fill('将第一台设备可用率提高到90%，保留其余参数');
    await page.locator('#send-prompt').click();
    const request = await intercepted;
    assert.equal(request.ai_profile_id, profiles[0]);
    assert.equal(await paused(), '播放仿真回放');
    const waitingIndex = await frame();
    await page.waitForTimeout(450);
    assert.equal(await frame(), waitingIndex);
    assert.equal(await page.locator('#adjust-progress').isVisible(), true);
    assert.equal(await page.locator('#prompt').isDisabled(), true);
    // Switching while a request is pending affects the next instruction only.
    await page.locator('#ai-profile-select').selectOption(profiles[1]);
    await page.waitForFunction(() => document.querySelector('#ai-hint').textContent.includes('fixture-workspace-1'));
    releaseAdjustment();
    const adjustmentResponse = await adjustmentReady;
    assert.equal(adjustmentResponse.ok(), true, await adjustmentResponse.text());
    await page.unroute(adjustmentPattern, holdAdjustment);
    await page.locator('#apply-adjustment').waitFor({state:'visible'});
    assert.equal(await page.evaluate(()=>state.session.active_version_id),editedSession.active_version_id);
    const application = page.waitForResponse(response=>response.url().endsWith('/apply') && response.request().method()==='POST');
    await page.locator('#apply-adjustment').click();
    const adjustedSession = await (await application).json();
    assert.notEqual(adjustedSession.active_version_id, editedSession.active_version_id);
    assert.equal(adjustedSession.versions.at(-1).ai_config.model, 'fixture-workspace-0');
    assert.equal(adjustedSession.versions.at(-1).config.machines[0].availability, .9);
    await waitReady();
    await waitPlaying();
    assert.match(await page.locator('#messages').textContent(), /fixture-workspace-0/);
    checks.push('sending pauses immediately; model is snapshotted; explicit confirmation applies and autoplays');

    // An unavailable provider cannot erase the existing preview or silently change the model.
    await page.route(adjustmentPattern, route => route.fulfill({
      status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '工作台测试：模型服务暂时不可用' }),
    }));
    await seek(.25);
    await page.locator('#play-pause').click();
    await page.locator('#prompt').fill('将第一台设备可用率提高到90%');
    await page.locator('#send-prompt').click();
    await page.waitForFunction(() => document.querySelector('#adjustment-status').textContent.includes('模型服务暂时不可用'));
    assert.equal(await paused(), '播放仿真回放');
    assert.equal(await page.locator('#play-pause').isDisabled(), false);
    const persisted = await (await page.request.get(`${url}/api/studio/sessions/${sessionId}`)).json();
    assert.equal(persisted.active_version_id, adjustedSession.active_version_id);
    before = await frame();
    await page.locator('#play-pause').click();
    await waitAdvance(before);
    await page.unroute(adjustmentPattern);
    await ensurePaused();
    checks.push('AI failure preserves model and resumable preview');

    // Finish by natural playback from near the end, not by mutating internal state.
    await page.locator('#seek').evaluate(element => {
      element.value = String(Math.max(0, Number(element.max) - 2));
      element.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.locator('#playback-speed').selectOption('10');
    await page.locator('#play-pause').click();
    await page.waitForFunction(() => document.querySelector('#seek').value === document.querySelector('#seek').max);
    await page.waitForFunction(() => !document.querySelector('#completion-banner').hidden);
    assert.equal(await page.locator('#results-dialog').evaluate(element => element.open), false,
      'completion should not steal focus with a modal');
    await page.waitForFunction(() => !document.querySelector('#study-summary').hidden, null, { timeout: 60000 });
    assert.equal(studies.filter(item => item.version_id === adjustedSession.active_version_id).length, 1);
    await page.locator('#view-final-results').click();
    assert.equal(await page.locator('#results-dialog').evaluate(element => element.open), true);
    assert.match(await page.locator('#result-cards').textContent(), /\d/);
    const evaluatedSession = await (await page.request.get(`${url}/api/studio/sessions/${sessionId}`)).json();
    const studyMetadata = evaluatedSession.runs.find(item => item.version_id === adjustedSession.active_version_id &&
      item.kind === 'study' && item.status === 'succeeded');
    assert.ok(studyMetadata, 'final cards must be backed by a completed study');
    const studyResult = await (await page.request.get(`${url}/api/studio/sessions/${sessionId}/runs/${studyMetadata.id}`)).json();
    const displayedMeans = await page.locator('#result-cards strong').allTextContents();
    assert.equal(displayedMeans.length, 3);
    ['throughput_per_hour', 'avg_wip', 'specific_energy_kwh_per_part'].forEach((metric, index) => {
      const actual = Number(displayedMeans[index].replaceAll(',', ''));
      const expected = studyResult.result.summary.find(row => row.metric === metric).mean;
      assert.ok(Number.isFinite(actual) && Math.abs(actual - expected) <= .00501,
        `${metric}: final card must match the evaluated mean`);
    });
    assert.match(await page.locator('#result-evaluation').textContent(), /\S/);
    assert.match(await page.locator('#study-summary').textContent(), /2 次/);
    await screenshot('workspace-final-results-qa.png');
    const report = await download('#download-report', '.html');
    const reportText = fs.readFileSync(report, 'utf8');
    assert.match(reportText, /<!doctype html>/i);
    assert.match(reportText, /置信|评估|方案/);
    assert.equal(reportText.includes('fixture-workspace-key-'), false);
    await page.locator('#close-results').click();
    await page.locator('#seek').evaluate(element => {
      element.value = String(Number(element.max) - 1);
      element.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.locator('#play-pause').click();
    await page.waitForFunction(() => document.querySelector('#seek').value === document.querySelector('#seek').max);
    await page.waitForTimeout(500);
    assert.equal(studies.filter(item => item.version_id === adjustedSession.active_version_id).length, 1,
      'replaying the end must reuse the completed study');
    checks.push('natural completion runs one full study, displays final metrics, and exports an HTML report');

    // Native host minimum window is 960×680; its primary controls must still fit.
    if (await page.evaluate(() => Boolean(document.fullscreenElement))) {
      await page.evaluate(() => document.exitFullscreen());
    }
    await page.setViewportSize({ width: 960, height: 680 });
    if (!await page.locator('body').evaluate(element => element.classList.contains('immersive'))) {
      await page.locator('#fullscreen-toggle').click();
    }
    await layout('960x680 immersive');
    await screenshot('workspace-minimum-desktop-qa.png');
    await page.locator('#workspace-exit').click();
    assert.equal(await page.locator('body').evaluate(element => element.classList.contains('immersive')), false);
    if (await page.evaluate(() => Boolean(document.fullscreenElement))) await page.evaluate(() => document.exitFullscreen());
    await page.setViewportSize({ width: 1440, height: 980 });
    checks.push('fullscreen exit and native minimum window layout');

    await download('#export-session', '.zip');
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('#model-name')?.value);
    assert.equal(await page.evaluate(() => localStorage.getItem('simlab.studio.session')), sessionId);
    assert.equal(await page.locator('#until-days').inputValue(), '0.05');
    await page.locator('#ai-settings').click();
    await page.waitForFunction(() => document.querySelector('#ai-model').value === 'fixture-workspace-1');
    await page.waitForFunction(() => document.querySelector('#ai-api-key').value === 'fixture-workspace-key-1');
    assert.equal(await page.locator('#ai-api-key').getAttribute('type'), 'password');
    await closeDialog('settings-dialog');
    const catalogText = await (await page.request.get(`${url}/api/studio/ai/profiles`)).text();
    assert.equal(catalogText.includes('fixture-workspace-key-'), false);
    await results();
    const restoring = page.waitForResponse(response => response.url().endsWith('/restore') &&
      response.request().method() === 'POST');
    await page.locator('.version-restore').last().click();
    const restored = await (await restoring).json();
    assert.equal(restored.versions.length, adjustedSession.versions.length + 1);
    await closeDialog('results-dialog');
    await waitReady();
    await ensurePaused();
    assert.equal(await page.locator('#until-days').inputValue(), '30');
    await openModel();
    const customConfig = structuredClone(exportedModel.config);
    customConfig.buffers[0].capacity = 8;
    const importing = page.waitForResponse(response => response.url().endsWith('/versions') &&
      response.request().method() === 'POST');
    await page.locator('#config-file').setInputFiles({
      name: 'workspace-custom.json', mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({ mode: 'custom', config: customConfig })),
    });
    assert.equal((await importing).status(), 200);
    await page.waitForFunction(() => document.querySelector('#model-mode').value === 'custom');
    assert.equal(await page.locator('#buffer-0-capacity').inputValue(), '8');
    await closeDialog('model-dialog');
    await waitReady();
    await ensurePaused();
    checks.push('ZIP export, persisted experiment, empty displayed key, version restore, and JSON import');

    // Embedded hosts may not expose Fullscreen API. They still fill the app viewport.
    const fallbackContext = await browser.newContext({ viewport: { width: 960, height: 680 } });
    try {
      await fallbackContext.addInitScript(() => {
        Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, get: () => false });
      });
      const fallbackPage = await fallbackContext.newPage();
      fallbackPage.on('pageerror', error => errors.push(error.message));
      await fallbackPage.goto(url, { waitUntil: 'networkidle' });
      await fallbackPage.waitForFunction(() => document.querySelector('#model-name')?.value);
      await fallbackPage.locator('#edit-model').click();
      await fallbackPage.locator('#until-days').fill('0.05');
      await fallbackPage.locator('#warmup-days').fill('0.005');
      await fallbackPage.locator('#replications').fill('2');
      await fallbackPage.locator('#apply-model').click();
      await fallbackPage.waitForFunction(() => !document.querySelector('#play-pause').disabled,
        null, { timeout: 60000 });
      assert.equal(await fallbackPage.evaluate(() => document.fullscreenElement), null);
      assert.equal(await fallbackPage.locator('body').evaluate(element => element.classList.contains('immersive')), true);
      await fallbackPage.locator('#workspace-exit').click();
      assert.equal(await fallbackPage.locator('body').evaluate(element => element.classList.contains('immersive')), false);
      checks.push('unsupported Fullscreen API falls back to an immersive embedded workspace');
    } finally { await fallbackContext.close(); }

    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'workspace-check.json'), JSON.stringify({
      ok: true, fixture: 'injected test agent, no external LLM calls',
      session_id: sessionId, checks, studies, artifacts, page_errors: errors,
    }, null, 2));
    console.log(JSON.stringify({ ok: true, output, session_id: sessionId, checks: checks.length }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'workspace-failure-qa.png'), fullPage: true }).catch(() => {});
    fs.writeFileSync(path.join(output, 'workspace-failure.json'), JSON.stringify({
      ok: false, error: String(error.stack || error), checks, studies, page_errors: errors,
    }, null, 2));
    throw error;
  } finally {
    releaseAdjustment?.();
    releasePreview?.();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
