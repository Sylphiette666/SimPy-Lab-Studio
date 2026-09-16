/* Visual-model workflow against the isolated injected-agent fixture. No real AI calls. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const url = process.env.STUDIO_TEST_URL;
  if (!url) throw new Error('Use an isolated injected-agent fixture.');
  const out = path.resolve(__dirname, '../outputs/visual-editor-qa'); fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const context = await browser.newContext({viewport:{width:1600, height:1020}});
  const page = await context.newPage(), errors = [], writes = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if(request.method()==='POST') writes.push(request.url());});
  const card = id => page.locator(`#graph-nodes [data-node-id="${id}"] .graph-node-card`);
  const port = (id, dir) => page.locator(`#graph-nodes [data-node-id="${id}"] .graph-port.${dir}`);
  const edgeCount = () => page.locator('#graph-edges g[data-edge-id]').count();
  async function connect(from, to, drag = false) {
    if (drag) await port(from,'out').dragTo(port(to,'in'));
    else {await port(from,'out').click(); await port(to,'in').click();}
  }
  async function removeEdge(from, to) {
    const edge = page.locator(`#graph-edges [data-edge-id="${from}:${to}"]`);
    await edge.focus(); await edge.press('Enter'); await page.locator('#graph-delete').click();
  }
  async function param(id, key, value) {
    await card(id).click(); await page.locator('#graph-param-'+key).fill(String(value));
    await page.locator('#graph-param-'+key).press('Tab');
  }
  try {
    assert.equal((await (await context.request.get(url+'/fixture-health')).json()).injected_test_agent, true);
    await page.goto(url, {waitUntil:'networkidle'});
    await page.waitForFunction(() => !document.querySelector('#edit-model').disabled);
    await page.locator('#edit-model').click();
    const modelName = await page.locator('#model-name').inputValue();
    await page.locator('#model-name').fill(''); await page.locator('#edit-model-graph').click();
    assert.equal(await page.locator('#visual-model-dialog').evaluate(item=>item.open),false);
    assert.equal(await page.locator('#model-error').isVisible(),true);
    assert.match(await page.locator('#model-error').textContent(),/模型名称/);
    await page.locator('#model-name').fill(modelName);
    await page.locator('#machine-0-cycle_time_seconds').fill('350');
    await page.locator('#until-days').fill('0.05'); await page.locator('#warmup-days').fill('0.005'); await page.locator('#replications').fill('2');
    const draft = await page.evaluate(() => readDraft()), initialSession = await page.evaluate(() => structuredClone(state.session));
    await page.locator('#edit-model-graph').click();
    assert.equal(await page.locator('.graph-node.machine').count(), 4);
    assert.equal(await page.locator('.graph-node.buffer').count(), 3); assert.equal(await edgeCount(), 8);
    const sourcePort = await port('source','out').boundingBox(), firstPort = await port('n1','in').boundingBox();
    await page.mouse.click((sourcePort.x+sourcePort.width/2+firstPort.x+firstPort.width/2)/2,sourcePort.y+sourcePort.height/2);
    assert.equal(await page.locator('#graph-selection-title').textContent(),'连接线');
    assert.equal(await page.locator('#graph-edges g.selected').getAttribute('data-edge-id'),'source:n1');
    await card('n1').click(); assert.equal(await page.locator('#graph-param-cycle_time_seconds').inputValue(),'350');
    const writesBefore = writes.length;
    const before = await card('n1').boundingBox();
    await page.mouse.move(before.x+60,before.y+40); await page.mouse.down(); await page.mouse.move(before.x+125,before.y+105,{steps:8}); await page.mouse.up();
    const after = await card('n1').boundingBox(); assert.ok(after.x>before.x+50 && after.y>before.y+50);
    assert.equal(await edgeCount(), 8);
    await page.locator('#graph-undo').click();
    assert.ok(Math.abs((await card('n1').boundingBox()).x-before.x)<2);
    await page.locator('#graph-redo').click(); assert.ok((await card('n1').boundingBox()).x>before.x+50);
    await page.locator('#graph-undo').click();
    await page.mouse.click(3,500);
    assert.equal(await page.locator('#visual-model-dialog').evaluate(item=>item.open),false);
    assert.equal(await page.locator('#model-dialog').evaluate(item=>item.open),true);
    assert.deepEqual(await page.evaluate(()=>readDraft()),draft);
    assert.equal(writes.length,writesBefore);
    checks.push('Existing unsaved input parameters load correctly; pointer movement updates wires, undo/redo work, backdrop closes without applying or dropping the input draft.');
    await page.locator('#edit-model-graph').click();
    await page.locator('#graph-add-machine').dragTo(page.locator('#graph-canvas'), {targetPosition:{x:845,y:470}});
    assert.equal(await page.locator('.graph-node.machine').count(),5);
    assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    await param('n8','name','清洗设备'); await param('n8','cycle_time_seconds',45);
    await page.locator('#graph-add-buffer').dragTo(page.locator('#graph-canvas'), {targetPosition:{x:610,y:470}});
    assert.equal(await page.locator('.graph-node.buffer').count(),4);
    await param('n9','name','清洗前容器'); await param('n9','capacity',8); await param('n9','delay_seconds',4);
    await removeEdge('n7','sink');
    await connect('n7','n9',true); await connect('n9','n8',true); await connect('n8','sink');
    assert.equal(await edgeCount(),10);
    assert.equal(await page.locator('#graph-apply').isDisabled(),false);
    assert.match(await page.locator('#graph-save-status').textContent(),/自定义实验/);
    await page.screenshot({path:path.join(out,'visual-model.png')});
    await connect('n8','sink'); assert.equal(await edgeCount(),10); assert.match(await page.locator('#graph-hint').textContent(),/已存在/);
    await page.keyboard.press('Escape'); // Cancel the pending connection, keeping editor open.
    assert.equal(await page.locator('#visual-model-dialog').evaluate(item=>item.open),true);
    await param('n8','name','SV36262'); assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    assert.match(await page.locator('#graph-issues').textContent(),/重复/);
    await param('n8','name','清洗设备');
    await param('n9','capacity','1.5'); assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    await param('n9','capacity',8);
    await param('n8','availability',90); assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    await param('n8','mttr_seconds',30); assert.equal(await page.locator('#graph-apply').isDisabled(),false);
    await card('n8').click(); await page.locator('#graph-delete').click();
    assert.equal(await page.locator('.graph-node.machine').count(),4);
    assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    await page.locator('#graph-undo').click(); assert.equal(await page.locator('#graph-apply').isDisabled(),false);
    assert.equal(writes.length,writesBefore);
    checks.push('Actual palette drags add independent nodes; actual port drags and click-to-connect create a valid serial extension; duplicate links and invalid parameters cannot be applied.');
    await page.locator('#close-visual-model').click();
    await page.locator('#edit-model-graph').click(); assert.equal(await page.locator('.graph-node.machine').count(),5);
    await page.locator('#graph-apply').click();
    assert.equal(await page.locator('#visual-model-dialog').evaluate(item=>item.open),false);
    assert.equal(await page.locator('#model-mode').inputValue(),'custom');
    const compiled = await page.evaluate(()=>readDraft());
    assert.equal(compiled.machines.at(-1).name,'清洗设备'); assert.equal(compiled.buffers.at(-1).name,'清洗前容器');
    assert.deepEqual(compiled.machines.slice(0,4),draft.machines);
    assert.equal(compiled.machines.at(-1).availability,.9); assert.equal(compiled.buffers.at(-1).capacity,8);
    for(const key of ['breaks','until_seconds','warmup_seconds','replications','base_seed','raw_buffer_capacity']) assert.deepEqual(compiled[key],draft[key]);
    assert.deepEqual(await page.evaluate(()=>state.session.versions), initialSession.versions);
    assert.equal(writes.length,writesBefore);
    await page.locator('#edit-model-graph').click();
    await page.locator('#graph-run').click();
    await page.waitForFunction(()=>state.playing && state.frames.length && !state.busy,null,{timeout:60000});
    assert.equal(await page.locator('#visual-model-dialog').evaluate(item=>item.open),false);
    const current = await page.evaluate(()=>activeVersion().config);
    assert.deepEqual(current, compiled);
    assert.equal(await page.evaluate(()=>state.frames[0].machines.length),5);
    checks.push('Applying transfers the exact connected model to the existing form without saving or running prematurely; Apply and Run creates a validated version and a real five-machine preview.');
    const beforeOpen = await page.evaluate(()=>state.frameIndex), runningWrites = writes.length;
    await page.locator('#open-visual-model').click();
    await card('n1').click();
    await page.waitForFunction(frame=>state.frameIndex>frame,beforeOpen);
    assert.equal(writes.length,runningWrites); assert.equal(await page.evaluate(()=>state.playing),true);
    const position = await card('n1').evaluate(item=>({x:item.parentElement.style.left,y:item.parentElement.style.top}));
    await card('n1').focus(); await page.keyboard.press('ArrowDown');
    assert.notDeepEqual(await card('n1').evaluate(item=>({x:item.parentElement.style.left,y:item.parentElement.style.top})),position);
    await page.locator('#graph-undo').click();
    for(const size of [{width:960,height:680},{width:390,height:740}]) {
      await page.setViewportSize(size);
      const fit = await page.locator('#visual-model-dialog').evaluate(item=>({width:item.scrollWidth,client:item.clientWidth,right:item.getBoundingClientRect().right,bottom:item.getBoundingClientRect().bottom,footer:document.querySelector('.visual-model-footer').getBoundingClientRect().bottom,canvas:document.querySelector('#graph-viewport').clientHeight}));
      assert.ok(fit.width<=fit.client+1 && fit.right<=size.width && fit.bottom<=size.height && fit.footer<=size.height && fit.canvas>100,JSON.stringify(fit));
      await page.screenshot({path:path.join(out,`visual-model-${size.width}.png`)});
    }
    await page.setViewportSize({width:1600,height:1020});
    await page.locator('#close-visual-model').click();
    if(await page.evaluate(()=>state.playing)) await page.locator('#play-pause').click();
    await page.reload({waitUntil:'networkidle'}); await page.waitForFunction(()=>state.session);
    await page.locator('#open-visual-model').click();
    assert.equal(await page.locator('.graph-node.machine').count(),5); assert.equal(await edgeCount(),10);
    assert.deepEqual(await card('n1').evaluate(item=>({x:item.parentElement.style.left,y:item.parentElement.style.top})),position);
    checks.push('Opening and editing a graph keeps an existing preview playing; keyboard movement works; the editor fits small windows and saved layout/model recover after reload.');
    // Keep the original source form intact if another input change arrived while this graph was open.
    await page.evaluate(()=>{document.querySelector('#machine-0-cycle_time_seconds').value='360';setDirty();});
    assert.equal(await page.locator('#graph-apply').isDisabled(),true);
    assert.equal(await page.locator('#graph-keep-draft').isVisible(),true);
    assert.equal(await page.locator('#visual-model-dialog').evaluate(item=>item.open),true);
    await page.locator('#graph-reset').click(); await card('n1').click();
    assert.equal(await page.locator('#graph-param-cycle_time_seconds').inputValue(),'360');
    // Server-level effort limits stay authoritative; a rejection must be visible after leaving the graph.
    await page.route('**/api/studio/sessions/*/versions', route => route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'隔离测试：实验计算规模超出限制。'})}));
    await page.locator('#graph-run').click();
    await page.waitForFunction(()=>!state.busy);
    assert.equal(await page.locator('#model-dialog').evaluate(item=>item.open),true);
    assert.match(await page.locator('#model-error').textContent(),/实验计算规模超出限制/);
    checks.push('Concurrent input changes cannot be overwritten; reloading reconciles them; backend validation errors remain visible in the input-model panel.');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'browser-checks.json'),JSON.stringify({ok:true,checks,errors},null,2)); console.log(JSON.stringify({ok:true,checks},null,2));
  } catch(error) {await page.screenshot({path:path.join(out,'failure.png')}).catch(()=>{}); throw error;}
  finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
