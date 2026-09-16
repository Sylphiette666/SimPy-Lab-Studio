/* Offline presentation checks. Explicit injected-agent fixture required; no paid AI. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('Set STUDIO_TEST_URL to the isolated fixture.');
  const output = path.resolve(__dirname, '../outputs/conversation-qa'); fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 980 }, acceptDownloads: true });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: url });
  const page = await context.newPage(), errors = [], remote = [], writes = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST') writes.push(request.url());
    if (!request.url().startsWith(url) && /^https?:/.test(request.url())) remote.push(request.url());
  });
  const code = 'def throughput(parts, hours):\n    return parts / hours\n';
  const rich = String.raw`### 方案与评估

建议提高**产出率**，同时比较能耗。

| 指标 | 原方案 | 新方案 |
| --- | ---: | ---: |
| 产出率 | 8.29 | 9.78 |
| 单位能耗 | 14.02 | 12.95 |

行内公式：\(E_{unit}=\frac{E}{N}\)，以及 $x_1 + x_2$。

$$
T = \frac{N_{out}}{t}
$$

\[
\sum_{i=1}^{n} x_i
\]

> 先评估，再确认改进。

1. 对比产出率
2. 记录设备可用率

代码中的 $x$ 和 \(y\) 应保持原样：

` + '```python\n' + code + '```\n\n' + '`$literal$ \\(code\\)`\n\n' +
    String.raw`无法识别的公式仍保留原文：$\notacommand{x}$。

安全示例：<img src="https://invalid.example/tracker" onerror="window.chatInjected=true">

[危险链接](javascript:alert(1))、[本机操作](/api/studio/sessions)、[正常链接](https://example.com/)

![外部图片](https://invalid.example/remote.png)

$\href{javascript:alert(1)}{unsafe}$

<script>window.chatInjected=true</script>`;
  const conversation = [
    { role: 'user', content: '提高产出率，保留代码 [x] 和输入原文 **不要改写**。', created_at: '2026-09-15T03:00:00Z' },
    { role: 'assistant', content: rich, applied: true, ai_config: { profile_name: '测试模型', model: 'fixture-only', api_key: 'secret-not-exported', protected_keys: 'cipher-not-exported' } },
    { role: 'error', content: '未应用这一次修改 <b>原文</b>。', applied: false },
    ...Array.from({ length: 16 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: `第 ${i + 4} 条说明\n` + '检查加工时间、在制品和能耗，不更改实验口径。\n'.repeat(10) })),
    { role: 'assistant', content: '最后一条产出率建议，复制与导出必须完整。', applied: false },
  ];
  try {
    assert.equal((await (await context.request.get(url + '/fixture-health')).json()).injected_test_agent, true);
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => !document.querySelector('#edit-model').disabled);
    await page.evaluate(() => { state.session.messages = []; state.pendingMessages = []; renderMessages(); });
    assert.equal(await page.locator('#chat-export').isDisabled(), true);
    await page.evaluate(items => { state.session.messages = items; renderMessages(); }, conversation);
    await page.locator('#prompt').fill('尚未发送的草稿');
    const writesBefore = writes.length;
    await page.locator('#expand-assistant').click();
    const answer = page.locator('.message').nth(1);
    await answer.scrollIntoViewIfNeeded();
    assert.equal(await answer.locator('table tbody tr').count(), 2);
    assert.equal(await answer.locator('h3').textContent(), '方案与评估');
    assert.equal(await answer.locator('pre code').textContent(), code);
    assert.ok(await answer.locator('.katex').count() >= 4);
    assert.match(await answer.locator('.chat-math-fallback').first().textContent(), /notacommand/);
    assert.equal(await page.locator('.message').first().locator('strong').count(), 0, 'User prompts keep exact plain text.');
    assert.equal(await answer.locator('script,img,iframe,object,form,input').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.chatInjected)), false);
    assert.equal(await answer.locator('a[href^="javascript"],a[href^="/"]').count(), 0);
    assert.equal(await answer.locator('a[href="https://example.com/"]').getAttribute('rel'), 'noopener noreferrer');
    checks.push('Offline Markdown tables/lists/code and both LaTeX delimiter styles render; unsupported math keeps its source; hostile markup stays inert.');
    await page.locator('#chat-search-toggle').click();
    await page.locator('#chat-search').fill('产出率');
    await page.waitForFunction(() => document.querySelector('#chat-search-count').textContent === '1 / 3 条');
    await page.locator('#chat-search-next').click();
    assert.equal(await page.locator('#chat-search-count').textContent(), '2 / 3 条');
    assert.equal(await page.locator('.chat-search-current').getAttribute('data-message-index'), '1');
    assert.ok(await answer.locator('mark').count() >= 2);
    await page.locator('#chat-search').press('Enter');
    assert.equal(await page.locator('#chat-search-count').textContent(), '3 / 3 条');
    await page.locator('#chat-search').press('Shift+Enter');
    assert.equal(await page.locator('#chat-search-count').textContent(), '2 / 3 条');
    await page.locator('#chat-search').fill('N_{out}');
    await page.waitForFunction(() => document.querySelector('#chat-search-count').textContent === '1 / 1 条');
    assert.equal(await answer.locator('.chat-math-match').count(), 1);
    await page.locator('#chat-search').fill('[x]');
    await page.waitForFunction(() => document.querySelector('.chat-search-current')?.dataset.messageIndex === '0');
    assert.equal(await page.locator('.message').first().locator('mark').textContent(), '[x]');
    await page.locator('#chat-search').fill('不存在的关键词');
    await page.waitForFunction(() => document.querySelector('#chat-search-count').textContent === '无匹配');
    assert.equal(await page.locator('#chat-search-next').isDisabled(), true);
    assert.equal(await page.locator('.message').count(), conversation.length);
    await page.locator('#chat-search').press('Escape');
    assert.equal(await page.locator('#assistant-dialog').evaluate(item => item.open), true);
    assert.equal(await page.locator('#chat-search').inputValue(), '');
    checks.push('Literal search navigates matching messages, including emphasis-spanning text and formula source; Escape clears and no-match keeps the full conversation.');
    await answer.locator('.chat-copy-message').click();
    // Windows clipboard normalizes line endings to CRLF; characters and indentation must match.
    assert.equal((await page.evaluate(() => navigator.clipboard.readText())).replaceAll('\r\n', '\n'), rich);
    await answer.locator('.chat-copy-code').click();
    assert.equal((await page.evaluate(() => navigator.clipboard.readText())).replaceAll('\r\n', '\n'), code);
    // Exercise WebView's denied/unavailable async clipboard path inside the modal.
    await page.evaluate(() => Object.defineProperty(navigator.clipboard, 'writeText', { configurable: true, value: () => Promise.reject(new Error('fixture-denied')) }));
    await page.locator('.message').first().locator('.chat-copy-message').click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), conversation[0].content);
    assert.equal(await page.locator('#prompt').inputValue(), '尚未发送的草稿');
    checks.push('Message and code copy preserve exact originals; the desktop clipboard fallback works inside the enlarged dialog without losing the draft.');
    await page.locator('#chat-search').fill('不存在的关键词');
    await page.waitForFunction(() => document.querySelector('#chat-search-count').textContent === '无匹配');
    for (const format of ['md', 'txt', 'json']) {
      await page.locator('#chat-export-format').selectOption(format);
      const ready = page.waitForEvent('download'); await page.locator('#chat-export').click();
      const file = await ready; assert.ok(file.suggestedFilename().endsWith('.' + format));
      const location = path.join(output, file.suggestedFilename()); await file.saveAs(location);
      const content = fs.readFileSync(location, 'utf8');
      assert.ok(!content.includes('secret-not-exported') && !content.includes('cipher-not-exported'));
      if (format === 'json') {
        const exported = JSON.parse(content);
        assert.deepEqual(exported.messages.map(item => item.content), conversation.map(item => item.content));
        assert.equal(exported.messages[1].model, '测试模型 · fixture-only');
      } else for (const message of conversation) assert.ok(content.includes(message.content));
    }
    checks.push('Markdown, TXT and JSON exports contain all original messages, statuses and model attribution regardless of search, without profile secrets.');
    await page.locator('#chat-search-clear').click();
    await page.locator('#messages').evaluate(item => { item.scrollTop = 150; window.chatScroll = item.scrollTop; window.chatFirst = item.firstElementChild; });
    await page.evaluate(() => renderMessages());
    assert.equal(await page.evaluate(() => document.querySelector('#messages').firstElementChild === window.chatFirst), true);
    await page.evaluate(() => { state.session.messages.push({role:'assistant', content:'新增回复不会把阅读中的用户拉到底部。'}); renderMessages(); });
    assert.ok(Math.abs(await page.locator('#messages').evaluate(item => item.scrollTop) - 150) < 2);
    await page.locator('#chat-latest').click();
    assert.ok(await page.locator('#messages').evaluate(item => item.scrollHeight - item.clientHeight - item.scrollTop < 2));
    await page.locator('#chat-search').fill('产出率');
    await page.waitForFunction(() => document.querySelector('#chat-search-count').textContent === '1 / 3 条');
    await page.locator('#chat-search-next').click();
    await page.screenshot({ path: path.join(output, 'assistant-expanded.png') });
    for (const size of [{ width: 960, height: 680 }, { width: 390, height: 740 }]) {
      await page.setViewportSize(size);
      const layout = await page.evaluate(() => {
        const dialog = document.querySelector('#assistant-dialog'), box = dialog.getBoundingClientRect();
        const send = document.querySelector('#send-prompt').getBoundingClientRect();
        return { overflow: dialog.scrollWidth > dialog.clientWidth + 1, right: box.right, left: box.left, sendBottom: send.bottom,
          messagesHeight: document.querySelector('#messages').clientHeight };
      });
      assert.ok(!layout.overflow && layout.left >= 0 && layout.right <= size.width && layout.sendBottom <= size.height && layout.messagesHeight > 80, JSON.stringify(layout));
      await page.screenshot({ path: path.join(output, `assistant-${size.width}.png`) });
    }
    await page.setViewportSize({ width: 1440, height: 980 });
    await page.locator('#expand-assistant').click();
    assert.equal(await page.locator('#chat-search').inputValue(), '产出率');
    assert.equal(await page.locator('#chat-search-count').textContent(), '2 / 3 条');
    assert.equal(await page.locator('#prompt').inputValue(), '尚未发送的草稿');
    await page.screenshot({ path: path.join(output, 'assistant-docked.png') });
    assert.equal(writes.length, writesBefore, 'Reading/search/copy/export cannot send prompts or mutate the model.');
    await page.evaluate(() => { const other = structuredClone(state.session); other.id = 'other-fixture-session'; other.messages = []; acceptSession(other); });
    assert.equal(await page.locator('#chat-search').inputValue(), '');
    assert.equal(await page.locator('#chat-export').isDisabled(), true);
    checks.push('Reading position survives rerenders; latest-message jump works; search/draft persist across enlarge/collapse, reset between experiments and fit narrow windows.');
    await page.evaluate(async () => {
      const config = structuredClone(state.bootstrap.template);
      Object.assign(config, {until_seconds: 7200, warmup_seconds: 0, replications: 2});
      acceptSession(await api('/sessions', {method: 'POST', body: {config, mode: 'paper'}}));
      await startRun('preview');
    });
    await page.waitForFunction(() => state.playing && state.frames.length > 0);
    await page.evaluate(() => { state.session.messages = [{role: 'assistant', content: '仿真继续运行，搜索不打断。'}]; renderMessages(); });
    const runningWrites = writes.length;
    await page.locator('#expand-assistant').click();
    await page.locator('#chat-search').fill('继续运行');
    await page.waitForFunction(() => document.querySelector('#chat-search-count').textContent === '1 / 1 条');
    await page.locator('.chat-copy-message').click();
    const ready = page.waitForEvent('download'); await page.locator('#chat-export').click(); await ready;
    const frame = await page.evaluate(() => state.frameIndex);
    await page.waitForFunction(before => state.frameIndex > before, frame);
    assert.equal(await page.evaluate(() => state.playing), true);
    assert.equal(writes.length, runningWrites);
    checks.push('Search, copy and export during a real sampled preview keep playback advancing and enqueue no AI or model requests.');
    assert.deepEqual(remote, [], 'Rendering must not load remote images, fonts or scripts.');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser-checks.json'), JSON.stringify({ ok: true, checks, errors, remote }, null, 2));
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } catch (error) { await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {}); throw error; }
  finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
