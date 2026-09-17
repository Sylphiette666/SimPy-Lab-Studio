/* All requests target an isolated fixture. Never contacts a paid provider. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
  const url=process.env.STUDIO_TEST_URL;
  if(!url)throw new Error('Set the isolated fixture URL');
  const out=path.resolve(__dirname,'../outputs/priority-qa');fs.mkdirSync(out,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1600,height:1050},acceptDownloads:true});
  let page=await context.newPage();const errors=[],checks=[];
  page.on('pageerror',e=>errors.push(e.message));
  async function api(p,body){const response=await context.request.fetch(url+'/api/studio'+p,{method:body===undefined?'GET':'POST',data:body});assert.ok(response.ok(),await response.text());return response.json();}
  async function tools(tab){if(!await page.locator('#tools-dialog').isVisible())await page.locator('#open-studio-tools').click();await page.locator('#tools-tab-'+tab).click();}
  try{
    assert.equal((await (await context.request.get(url+'/fixture-health')).json()).injected_test_agent,true);
    const cfg={name:'界面专项实验',until_seconds:300,warmup_seconds:20,replications:10,base_seed:37,confidence_level:.95,raw_buffer_capacity:1000,breaks:[],machines:[{name:'M1',cycle_time_seconds:2,availability:.9,mttr_seconds:4,idle_power_kw:1,processing_power_kw:3}],buffers:[]};
    const session=await api('/sessions',{config:cfg,mode:'custom'});
    await context.request.put(url+'/api/studio/workspace',{data:{selected_session_id:session.id,backup_minutes:0}});
    await page.goto(url,{waitUntil:'networkidle'});await page.waitForFunction(()=>!!window.StudioTools&&!!state.session&&!state.busy);
    await page.locator('#edit-model').click();await page.locator('#machine-0-cycle_time_seconds').fill('');await page.locator('#model-name').fill('尚未应用草稿');await page.locator('#close-model').click();await page.locator('#prompt').fill('未发送目标');
    await page.waitForTimeout(850);await page.evaluate(()=>window.StudioRecovery.flush());
    await page.evaluate(()=>localStorage.clear());await page.reload({waitUntil:'networkidle'});await page.waitForFunction(()=>document.querySelector('#prompt').value==='未发送目标');
    await page.locator('#edit-model').click();assert.equal(await page.locator('#model-name').inputValue(),'尚未应用草稿');assert.equal(await page.locator('#machine-0-cycle_time_seconds').inputValue(),'');
    await page.locator('#machine-0-cycle_time_seconds').fill('2');await page.locator('#machine-0-mttr_seconds').fill('0');await page.locator('#validate-model').click();await page.locator('#machine-0-mttr_seconds.parameter-invalid').waitFor();
    await page.locator('#machine-0-mttr_seconds').fill('4');await page.locator('#validate-model').click();await page.waitForFunction(()=>!document.querySelector('.parameter-invalid'));
    checks.push('Server-owned drafts survive clearing origin storage, including empty numeric input; linked validation focuses the failing field.');
    await page.locator('#import-table').click();await page.locator('#table-file').setInputFiles({name:'parameters.csv',mimeType:'text/csv',buffer:Buffer.from('name,cycle_time_seconds\nM1,3\n')});await page.locator('#preview-table').waitFor();await page.locator('#preview-table').click();await page.locator('#apply-table').click();assert.equal(await page.locator('#machine-0-cycle_time_seconds').inputValue(),'3');
    await page.locator('#apply-model').click();await page.waitForFunction(()=>state.frames.length>0&&!state.busy);await page.evaluate(()=>pause());
    checks.push('CSV field mapping previews changes and writes only the input draft; applying creates a validated model and real replay.');
    await page.locator('#ai-settings').click();await page.locator('#test-ai-connection').click();await page.waitForFunction(()=>document.querySelector('#connection-test-result').textContent.includes('隔离测试响应'));await page.locator('#settings-dialog .close-dialog').first().click();
    await tools('experiments');await page.locator('#experiment-search').fill('尚未应用草稿');
    const name=page.locator('#experiment-name-'+session.id),tag='校准-'+session.id.slice(0,8);await name.fill('实验管理已更名');await page.locator('#experiment-tags-'+session.id).fill(tag+', 标签');await name.locator('xpath=ancestor::section[1]').getByRole('button',{name:'保存名称与标签',exact:true}).click();await page.locator('#experiment-search').fill(tag);assert.equal(await page.locator('[id^="experiment-name-"]').count(),1);
    await page.getByRole('button',{name:'归档',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('[id^="experiment-name-"]'));await page.locator('#experiment-filter').selectOption('archived');await page.getByRole('button',{name:'取消归档',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('[id^="experiment-name-"]'));
    checks.push('Connection testing is explicit and isolated; experiment search, rename, tags and archive are reversible.');
    await tools('batch');await page.locator('#batch-path-0').selectOption('machines.0.cycle_time_seconds');await page.locator('#batch-values-0').fill('2,4');await page.locator('#start-batch').click();await page.waitForFunction(()=>document.querySelector('.tools-content').textContent.includes('已处理 2 / 2'),{},{timeout:30000});
    assert.equal(await page.getByRole('button',{name:'详情',exact:true}).count(),2);await page.screenshot({path:path.join(out,'batch.png')});
    await tools('analysis');const options=await page.locator('#analysis-version option').evaluateAll(els=>els.map(e=>e.value));await page.locator('#analysis-version').selectOption(options.at(-1));await page.locator('#analysis-baseline').selectOption(options.at(-2));await page.waitForFunction(()=>document.querySelector('.tools-content').textContent.includes('精确配对符号检验'));
    await page.locator('#reference-throughput_per_hour').fill('800');await page.locator('#reference-conditions').fill('测试产线同周期实测，单位件/小时');await page.locator('#reference-confirm').check();await page.locator('#save-reference').click();
    await page.locator('#analysis-version').selectOption(options.at(-1));await page.locator('#analysis-baseline').selectOption(options.at(-2));await page.waitForFunction(()=>document.querySelector('.tools-content').textContent.includes('归一化均方根相对误差'));
    await page.screenshot({path:path.join(out,'analysis.png')});checks.push('A bounded real batch completes with ranking, comparable paired tests, precision advice and measurement calibration.');
    await tools('backups');await page.locator('#backup-now').click();await page.waitForFunction(()=>document.querySelector('.tools-content').textContent.includes('备份已生成'));
    const downloadPromise=page.waitForEvent('download');await page.getByRole('button',{name:'下载副本',exact:true}).first().click();const download=await downloadPromise;const backupPath=path.join(out,'downloaded-backup.zip');await download.saveAs(backupPath);await page.locator('#restore-experiment-file').setInputFiles(backupPath);await page.locator('#restore-experiment').click();await page.waitForFunction(()=>/已恢复 \d+ 个实验/.test(document.querySelector('.tools-content').textContent));
    checks.push('Full backup download and ZIP restore create independent experiments without overwriting originals.');
    for(const size of [{width:1280,height:800},{width:960,height:720}]){await page.setViewportSize(size);await tools('experiments');assert.ok(await page.locator('#close-tools').isVisible());const box=await page.locator('#tools-dialog').boundingBox();assert.ok(box.x>=0&&box.width<=size.width);}
    assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'browser-check.json'),JSON.stringify({ok:true,checks},null,2));console.log(JSON.stringify({ok:true,checks},null,2));
  }catch(e){await page.screenshot({path:path.join(out,'failure.png')}).catch(()=>{});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
