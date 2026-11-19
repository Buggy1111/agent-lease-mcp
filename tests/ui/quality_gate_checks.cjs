const assert=require('node:assert/strict');
const path=require('node:path');
module.exports=async({page,check,load,text,calls,outDir,fixture,setSnapshot,emit,wait})=>{
  setSnapshot(fixture()); await page.setViewportSize({width:1920,height:1080}); await load('/after');
  await check('Quality Gate starts empty, never treats reviews or task success as quality results',async()=>{
    assert((await text(page,'#qgOverviewPanel')).includes('Dosud bez výsledků'));
    assert.equal(await page.locator('#qgCompactSummary strong').first().textContent(),'—');
    await page.selectOption('#taskPicker','13'); assert((await text(page,'.task-quality')).includes('Kontrola zatím není dostupná'));
    assert.equal(await page.locator('.task-quality').getByText('PASS',{exact:true}).count(),0);
    await page.locator('.primary-nav [data-view="quality"]').click();
    assert(await page.locator('#qualityPage').isVisible()); assert.equal(await text(page,'#qgRepoCount'),'—');
    assert((await text(page,'#qgProjectTable')).includes('Dosud bez výsledků'));
    assert(await page.locator('#qualityTitle').evaluate(el=>el===document.activeElement));
  });
  await check('Explicit demo presents every result type, repository details and independent review without backend writes',async()=>{
    const before=calls.length;
    await page.locator('#qualityPage [data-qg-demo]').click();
    assert.equal(await text(page,'#qgRepoCount'),'6');
    assert((await text(page,'#qualityPage [data-qg-mode]')).includes('DEMO'));
    for(const state of ['PASS','REVIEW','FAIL','N/A','SETUP REQUIRED','NOT RUN']) assert((await text(page,'#qgProjectTable')).includes(state),state);
    await page.locator('#qgProjectTable [data-qg-project="0"]').click();
    assert.equal(await text(page,'#qgProjectTitle'),'demo/agent-lease');
    const detail=await text(page,'#qgProjectDetail');
    for(const field of ['Commit','Coverage','Type check','CRAP score','Protokoly','Historie kontrol','Nezávislé review','DEMO']) assert(detail.includes(field),field);
    await page.locator('#qgProjectTable [data-qg-project="1"]').click();
    assert((await text(page,'#qgProjectTitle')).includes('velmi-dlouhym'));
    await page.locator('.primary-nav [data-view="overview"]').click();
    assert((await text(page,'.task-quality')).includes('Kontrola zatím není dostupná'));
    assert.equal(await page.locator('.task-quality').getByText('PASS',{exact:true}).count(),0);
    assert.equal(calls.length,before);
  });
  await check('Quality tabs, Analytics/Settings placeholders, mobile overflow and keyboard navigation',async()=>{
    for(const view of ['analytics','settings','quality','overview']) {
      await page.locator(`.primary-nav [data-view="${view}"]`).focus(); await page.keyboard.press('Enter');
      assert(await page.locator(`[data-view-panel="${view}"]`).isVisible());
      assert.equal(await page.locator('.primary-nav [aria-current="page"]').count(),1);
    }
    await page.locator('.primary-nav [data-view="quality"]').click();
    for(const width of [1920,1366,768,390,320]) {
      await page.setViewportSize({width,height:1080});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'quality page overflow '+width);
      assert(await page.locator('#qualityPage .qg-scroll').evaluate(el=>el.scrollWidth>=el.clientWidth));
      assert(await page.getByRole('button',{name:'Zastavit všechny agenty'}).isVisible());
    }
    await page.locator('#qualityPage [data-qg-demo]').click(); assert.equal(await text(page,'#qgRepoCount'),'—');
    assert.equal(await page.locator('#qgProjectTable [data-qg-project]').count(),0);
    await page.locator('.primary-nav [data-view="overview"]').click();
  });
  await check('Live quality runs arrive through existing snapshot/SSE, stay separate from demo and escape untrusted labels',async()=>{
    const live={kind:'quality_run',executed:true,run_id:101,repository:'fixture/<img src=x onerror="window.injected=1">',commit_sha:'a'.repeat(40),agent:'codex',timestamp:'2026-11-07T12:00:00Z',overall_status:'REVIEW',checks:{tests:{status:'PASS',detail:'Configured command exited successfully.'},security:{status:'REVIEW'}},review_status:'pending',history:[]};
    const s={...fixture(),quality_runs:[live,{kind:'inspection',executed:false,repository:'MUST_NOT_RENDER',overall_status:'PASS'}]};
    setSnapshot(s);await load('/after');
    await page.locator('.primary-nav [data-view="quality"]').click();
    assert.equal(await text(page,'#qgRepoCount'),'1');
    assert(!(await text(page,'#qgProjectTable')).includes('MUST_NOT_RENDER'));
    assert.equal(await page.locator('#qgProjectTable img').count(),0);
    await page.locator('#qgProjectTable [data-qg-project="0"]').click();
    assert((await text(page,'#qgProjectDetail')).includes('Čeká na nezávislé review'));
    assert(!(await text(page,'#qgProjectDetail')).includes('DEMO'));
    const before=calls.length;
    await page.locator('#qualityPage [data-qg-demo]').click();
    assert.equal(await text(page,'#qgRepoCount'),'6');
    const updated={...live,overall_status:'FAIL',checks:{...live.checks,tests:{status:'FAIL'}}};
    emit('room',{...s,quality_runs:[updated]});
    await wait(()=>page.evaluate(()=>qgRecords?.[0]?.overall_status==='FAIL'),'quality SSE in demo');
    assert.equal(await text(page,'#qgRepoCount'),'6');
    await page.locator('#qualityPage [data-qg-demo]').click();
    assert.equal(await text(page,'#qgRepoCount'),'1');
    assert((await text(page,'#qgProjectTable')).includes('FAIL'));
    // Keep selection bound to a run id when a new repository is prepended.
    await page.locator('#qgProjectTable [data-qg-project="0"]').click();
    emit('room',{...s,quality_runs:[{...live,run_id:202,repository:'different'},updated]});
    await wait(async()=>await text(page,'#qgRepoCount')==='2','second quality repo');
    assert.equal(await text(page,'#qgProjectTitle'),live.repository);
    assert.equal(calls.length,before);
    emit('room',{...s,quality_runs:[]});
    await wait(async()=>await text(page,'#qgRepoCount')==='0','empty quality snapshot');
    assert((await text(page,'#qgProjectDetail')).includes('Vyber zaznamenaný běh'));
    assert.equal(await page.evaluate(()=>window.injected),undefined);
    setSnapshot(fixture());await load('/after');
  });
  await check('Quality requests, task binding, typecheck and safe redacted log links use live records',async()=>{
    await page.setViewportSize({width:1920,height:1080});
    const checks=Object.fromEntries(['tests','lint','build','security','typecheck'].map(name=>[name,{status:'PASS',required:true}]));
    const record={kind:'quality_run',executed:true,run_id:303,task_id:13,repository:'fixture/live',commit_sha:'c'.repeat(40),agent:'codex',overall_status:'REVIEW',checks,review_status:'pending',logs:[{check:'tests',url:'/api/quality-log?run_id=303&check=tests'},{check:'evil',url:'javascript:window.injected=1'}]};
    const s={...fixture(),quality_runs:[record],quality_requests:[{repository:'fixture/<img src=x onerror="window.injected=1">',state:'queued',detail:'fixture request'}]};
    setSnapshot(s);await load('/after');
    await page.selectOption('#taskPicker','13');
    assert((await text(page,'.task-quality')).includes(record.commit_sha));
    assert((await text(page,'.task-quality')).includes('REVIEW'));
    await page.locator('.primary-nav [data-view="quality"]').click();
    assert((await text(page,'#qgRequests')).includes('Čeká ve frontě'));
    assert.equal(await page.locator('#qgRequests img').count(),0);
    assert((await text(page,'#qgProjectTable')).includes('Type check'));
    await page.locator('#qgProjectTable [data-qg-project="0"]').click();
    assert.equal(await page.locator('#qgProjectDetail a').count(),1);
    assert.equal(await page.locator('#qgProjectDetail a').getAttribute('href'),'/api/quality-log?run_id=303&check=tests');
    assert.equal(await page.locator('#qgApprovePolicy').count(),0);
    setSnapshot(fixture());await load('/after');
  });
  await check('Critical approval requires explicit human dialogs and binds the exact run and commit',async()=>{
    const previousDialogs=page.listeners('dialog');
    page.removeAllListeners('dialog');
    const checks=Object.fromEntries(['tests','lint','build','security'].map(name=>[name,{status:'PASS',required:true}]));
    const record={kind:'quality_run',executed:true,run_id:404,repository:'fixture/critical',commit_sha:'d'.repeat(40),agent:'codex',overall_status:'REVIEW',checks,review_status:'approved',policy_review_required:true,policy_review_status:'pending',logs:[]};
    const s={...fixture(),quality_runs:[record],quality_requests:[]};
    setSnapshot(s);await load('/after');
    await page.locator('.primary-nav [data-view="quality"]').click();
    await page.locator('#qgProjectTable [data-qg-project="0"]').click();
    assert(await page.locator('#qgApprovePolicy').isVisible());
    const before=calls.length;
    page.once('dialog',dialog=>dialog.dismiss());
    await page.locator('#qgApprovePolicy').click();
    assert.equal(calls.length,before);
    const accept=dialog=>dialog.type()==='prompt' ? dialog.accept('fixture human approval reason') : dialog.accept();
    page.on('dialog',accept);
    await page.locator('#qgApprovePolicy').click();
    await wait(()=>calls.length===before+1,'explicit critical approval request');
    page.off('dialog',accept);
    assert.deepEqual(calls.at(-1),{url:'/api/quality-policy-approve',body:{run_id:404,commit_sha:record.commit_sha,reason:'fixture human approval reason'}});
    emit('room',{...s,quality_runs:[{...record,policy_review_status:'approved',overall_status:'PASS'}]});
    await wait(async()=>await page.locator('#qgApprovePolicy').count()===0,'critical approval removed after SSE');
    assert((await text(page,'#qgProjectDetail')).includes('Kritické změny schválil člověk'));
    previousDialogs.forEach(handler=>page.on('dialog',handler));
    setSnapshot(fixture());await load('/after');
  });
};
