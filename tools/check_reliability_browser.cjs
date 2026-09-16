/* Isolated regression: preserved drafts, explicit review, cancellation and recovery. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('Set STUDIO_TEST_URL to the injected test fixture.');
  const out = path.resolve(__dirname, '../outputs/reliability-qa'); fs.mkdirSync(out,{recursive:true});
  const browser = await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge',headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    assert.equal((await (await page.request.get(url+'/fixture-health')).json()).injected_test_agent,true);
    await page.goto(url); await page.waitForFunction(()=>!!state.session && !state.busy);
    await page.locator('#open-visual-model').click(); await page.locator('#graph-add-machine').click();
    await page.locator('#close-visual-model').click();
    await page.locator('#edit-model').click(); await page.locator('#model-name').fill('draft-conflict-review');
    await page.keyboard.press('Escape'); await page.locator('#open-visual-model').click();
    assert.equal(await page.locator('.graph-node.machine').count(),5);
    assert.equal(await page.locator('#graph-undo').isEnabled(),true);
    assert.equal(await page.locator('#graph-keep-draft').isVisible(),true);
    assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    await page.screenshot({path:path.join(out,'draft-conflict.png')});
    await page.reload(); await page.waitForFunction(()=>!!state.session && !state.busy);
    await page.locator('#open-visual-model').click();
    assert.equal(await page.locator('.graph-node.machine').count(),5);
    await page.locator('#graph-keep-draft').click();
    assert.equal(await page.locator('.graph-node.machine').count(),5);
    await page.locator('#graph-undo').click(); assert.equal(await page.locator('.graph-node.machine').count(),4);
    await page.locator('#graph-reset').click(); await page.locator('#close-visual-model').click();
    await page.locator('#edit-model').click();
    await page.locator('#machine-0-cycle_time_seconds').fill('');
    await page.locator('#model-name').fill('restore-incomplete-input');
    await page.keyboard.press('Escape'); await page.locator('#prompt').fill('尚未发送的目标');
    await page.reload(); await page.waitForFunction(()=>!!state.session && !state.busy);
    assert.equal(await page.locator('#prompt').inputValue(),'尚未发送的目标');
    await page.locator('#edit-model').click();
    assert.equal(await page.locator('#model-name').inputValue(),'restore-incomplete-input');
    assert.equal(await page.locator('#machine-0-cycle_time_seconds').inputValue(),'');
    await page.locator('#machine-0-cycle_time_seconds').fill('320');
    await page.locator('#until-days').fill('0.05'); await page.locator('#warmup-days').fill('0.005');
    await page.locator('#replications').fill('2'); await page.locator('#apply-model').click();
    await page.waitForFunction(()=>!state.busy && state.frames.length>0);
    const before = await page.evaluate(()=>state.session.versions.length);
    await page.locator('#prompt').fill('将第一台可用率提高到90%'); await page.locator('#send-prompt').click();
    await page.locator('#apply-adjustment').waitFor({state:'visible'});
    assert.equal(await page.evaluate(()=>state.session.versions.length),before);
    assert.match(await page.locator('#adjustment-changes').textContent(),/90/);
    await page.screenshot({path:path.join(out,'proposal-review.png')});
    // Ready proposals survive a browser restart and still require confirmation.
    await page.reload(); await page.waitForFunction(()=>!!state.session);
    await page.locator('#review-adjustment').click(); await page.locator('#apply-adjustment').click();
    await page.waitForFunction(n=>state.session.versions.length===n+1 && !state.adjusting,before);
    assert.equal(await page.evaluate(()=>activeVersion().config.machines[0].availability),.9);
    await page.waitForFunction(()=>state.frames.length>0);
    // Generate a valid proposal from a fresh experiment, then discard it.
    await page.locator('#new-session').click(); await page.waitForFunction(()=>!state.busy && state.session.versions.length===1);
    await page.locator('#prompt').fill('提高到90%'); await page.locator('#send-prompt').click();
    await page.locator('#discard-adjustment').waitFor({state:'visible'}); await page.locator('#discard-adjustment').click();
    await page.waitForFunction(()=>!state.adjusting);
    assert.equal(await page.evaluate(()=>state.session.versions.length),1);
    assert.equal(await page.evaluate(()=>state.session.adjustments.at(-1).status),'cancelled');
    await page.locator('#retry-adjustment').click(); await page.locator('#apply-adjustment').waitFor({state:'visible'});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('#apply-adjustment').isVisible(),true);
    await page.screenshot({path:path.join(out,'proposal-mobile.png')});
    await page.locator('#discard-adjustment').click();
    await page.waitForFunction(()=>!state.adjusting);
    // Keep the UI in the waiting phase while the backend cancellation remains real.
    const pendingPattern = '**/api/studio/sessions/*/adjustments/*';
    await page.route(pendingPattern, async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch(), body = await response.json();
      await route.fulfill({response,json:{...body,status:'running'}});
    });
    await page.locator('#retry-adjustment').click();
    await page.locator('#cancel-adjustment').click();
    await page.waitForFunction(()=>!state.adjusting);
    assert.equal(await page.evaluate(()=>state.session.adjustments.at(-1).status),'cancelled');
    assert.equal(await page.evaluate(()=>state.session.versions.length),1);
    await page.unroute(pendingPattern);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'checks.json'),JSON.stringify({ok:true,checks:[
      'Graph conflict preserves nodes and undo, blocks stale application and survives reload.',
      'Incomplete form input and unsent prompt survive reload.',
      'AI generation leaves model unchanged; saved proposal requires explicit application.',
      'Discard and cancellation while waiting leave the model unchanged; retry opens another review; narrow layout remains usable.'
    ]},null,2));
    console.log(JSON.stringify({ok:true,output:out}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
