/* Run against tests/studio_browser_fixture.py. No external AI calls. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
  const url=process.env.STUDIO_TEST_URL;
  if(!url) throw new Error('Set STUDIO_TEST_URL to the isolated test fixture.');
  const output=path.resolve(__dirname,'../outputs/topology_qa');fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:980},acceptDownloads:true});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const count=()=>page.locator('#machine-fields .machine-details').count();
  async function add(position,name,container){
    await page.locator('#add-equipment').click();
    if(position!==undefined)await page.locator('#equipment-position').selectOption(String(position));
    if(name)await page.locator('#equipment-name').fill(name);
    if(container)await page.locator('#container-name').fill(container);
    await page.locator('#confirm-equipment').click();
    await page.waitForFunction(()=>!document.querySelector('#equipment-dialog').open);
  }
  async function remove(index){
    const card=page.locator('#machine-fields .machine-details').nth(index);
    if(!await card.evaluate(el=>el.open))await card.locator('summary').click();
    await page.locator('.topology-remove').nth(index).click();
    await page.locator('#confirm-remove-equipment').click();
    await page.waitForFunction(()=>!document.querySelector('#remove-equipment-dialog').open);
  }
  try{
    const health=await (await page.request.get(url+'/fixture-health')).json();assert.equal(health.injected_test_agent,true);
    await page.goto(url,{waitUntil:'networkidle'});
    await page.waitForFunction(()=>document.querySelector('#model-name').value);
    await page.locator('#edit-model').click();
    assert.equal(await count(),4);assert.equal(await page.locator('#buffer-fields .buffer-row').count(),3);
    assert.equal(await page.locator('#machine-0-name').isDisabled(),true);
    assert.equal(await page.locator('.topology-remove').first().isDisabled(),true);
    await page.locator('#add-equipment').click();
    await page.locator('#equipment-name').fill('取消的设备');
    await page.locator('#equipment-dialog .close-dialog').last().click();
    assert.equal(await page.locator('#model-mode').inputValue(),'paper');
    assert.equal(await count(),4);
    assert.equal(await page.locator('#save-status').textContent(),'模型与结果保存在本机');
    await page.locator('#model-name').fill('设备与容器扩展验证');
    await page.locator('#until-days').fill('0.05');await page.locator('#warmup-days').fill('0.005');await page.locator('#replications').fill('2');
    await page.locator('#machine-0-cycle_time_seconds').fill('350');
    await page.locator('#machine-0-idle_power_kw').fill('9');
    await page.locator('#add-equipment').click();
    await page.locator('#equipment-name').fill(await page.locator('#machine-0-name').inputValue());
    await page.locator('#confirm-equipment').click();
    assert.match(await page.locator('#equipment-error').textContent(),/名称已存在/);
    await page.locator('#equipment-name').fill('清洗设备');
    await page.locator('#container-name').fill('清洗前暂存容器');
    await page.locator('#equipment-availability').fill('90');await page.locator('#equipment-mttr').fill('0');
    await page.locator('#confirm-equipment').click();
    assert.match(await page.locator('#equipment-error').textContent(),/维修时间必须大于/);
    await page.locator('#equipment-availability').fill('100');
    await page.locator('#equipment-cycle').fill('60');await page.locator('#container-capacity').fill('8');await page.locator('#container-delay').fill('4');
    await page.locator('#confirm-equipment').click();
    await page.waitForFunction(()=>!document.querySelector('#equipment-dialog').open);
    assert.equal(await count(),5);assert.equal(await page.locator('#buffer-fields .buffer-row').count(),4);
    assert.equal(await page.locator('#model-mode').inputValue(),'custom');
    assert.equal(await page.locator('#machine-4-name').inputValue(),'清洗设备');
    assert.equal(await page.locator('#buffer-3-name').inputValue(),'清洗前暂存容器');
    assert.equal(await page.locator('#buffer-3-capacity').inputValue(),'8');
    assert.equal(await page.locator('#machine-0-cycle_time_seconds').inputValue(),'350');
    assert.equal(await page.locator('#machine-0-idle_power_kw').inputValue(),'9');
    assert.equal(await page.locator('#until-days').inputValue(),'0.05');
    assert.equal(await page.locator('#model-name').inputValue(),'设备与容器扩展验证');
    assert.equal(await page.locator('#production-line [data-machine]').count(),4,'Unsaved topology must not change active replay');
    await page.locator('.topology-remove').nth(4).click();
    assert.match(await page.locator('#remove-equipment-description').textContent(),/清洗前暂存容器/);
    await page.locator('#remove-equipment-dialog .close-dialog').last().click();assert.equal(await count(),5);
    await remove(4);assert.equal(await count(),4);
    await add(1,'中间加工设备','中间出料容器');
    assert.equal(await page.locator('#machine-1-name').inputValue(),'中间加工设备');
    assert.equal(await page.locator('#buffer-0-name').inputValue(),'buffer1');
    assert.equal(await page.locator('#buffer-1-name').inputValue(),'中间出料容器');
    await add(0,'首端设备','首端容器');
    assert.equal(await page.locator('#machine-0-name').inputValue(),'首端设备');
    assert.equal(await page.locator('#buffer-0-name').inputValue(),'首端容器');
    await remove(0);assert.equal(await page.locator('#machine-1-name').inputValue(),'中间加工设备');
    await page.locator('#machine-fields .machine-details').nth(1).locator('summary').click();
    await page.locator('#machine-1-name').fill('精加工设备');await page.locator('#buffer-1-name').fill('精加工暂存容器');
    while(await count()<12)await add();
    assert.equal(await page.locator('#add-equipment').isDisabled(),true);
    assert.equal(await page.locator('#buffer-fields .buffer-row').count(),11);
    const saving=page.waitForResponse(r=>r.url().endsWith('/versions')&&r.request().method()==='POST');
    await page.locator('#apply-model').click();
    const response=await saving;assert.equal(response.ok(),true,await response.text());
    const session=await response.json();const config=session.versions.at(-1).config;
    assert.equal(config.machines.length,12);assert.equal(config.buffers.length,11);
    assert.equal(config.machines[1].name,'精加工设备');assert.equal(config.buffers[1].name,'精加工暂存容器');
    assert.equal(config.machines[0].cycle_time_seconds,350);assert.equal(config.machines[0].idle_power_kw,9);
    await page.waitForFunction(()=>!document.querySelector('#play-pause').disabled,null,{timeout:60000});
    if(await page.locator('#play-pause').getAttribute('aria-label')==='暂停仿真回放')await page.locator('#play-pause').click();
    assert.equal(await page.locator('#production-line [data-machine]').count(),12);
    assert.equal(await page.locator('#production-line [data-buffer]').count(),11);
    await page.locator('#seek').evaluate(el=>{el.value=el.max;el.dispatchEvent(new Event('input',{bubbles:true}));});
    assert.notEqual(await page.locator('#metric-wip').textContent(),'—');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
    await page.screenshot({path:path.join(output,'twelve-equipment-preview.png')});
    await page.locator('#nav-results').click();await page.locator('#run-study').click();
    await page.waitForFunction(()=>!document.querySelector('#study-summary').hidden,null,{timeout:60000});
    assert.match(await page.locator('#study-summary').textContent(),/2 次/);
    await page.locator('#close-results').click();
    await page.locator('#edit-model').click();
    const downloadReady=page.waitForEvent('download');await page.locator('#export-config').click();
    const download=await downloadReady;const modelPath=path.join(output,'expanded-model.json');await download.saveAs(modelPath);
    const exported=JSON.parse(fs.readFileSync(modelPath,'utf8'));assert.equal(exported.config.machines.length,12);
    await page.locator('#close-model').click();
    await page.reload({waitUntil:'networkidle'});await page.waitForFunction(()=>document.querySelector('#model-name').value);
    await page.locator('#edit-model').click();assert.equal(await count(),12);
    // Collapse to the minimum and make sure one machine/zero buffers remains valid.
    while(await count()>1)await remove((await count())-1);
    assert.equal(await page.locator('#buffer-fields .buffer-row').count(),0);
    assert.equal(await page.locator('.topology-remove').first().isDisabled(),true);
    await add(0,'首工位','工位间容器');assert.equal(await count(),2);
    await page.locator('#add-equipment').click();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'add-equipment-mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'topology-check.json'),JSON.stringify({ok:true,session_id:session.id,
      checks:['cancel preserves paper draft','paper names and removal locked','duplicate names and repair validation',
      'append with configured buffer','preserve unsaved scalar parameters','cancel and confirm removal',
      'insert at middle and start with stable existing buffers','rename equipment and containers','12-machine limit',
      'real 12-machine preview and replicated study','JSON export and reload','one-machine minimum','mobile dialog'],errors},null,2));
    console.log(JSON.stringify({ok:true,checks:13,session:session.id}));
  }catch(e){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});throw e;}
  finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
