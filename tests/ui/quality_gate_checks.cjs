const assert=require('node:assert/strict');
const path=require('node:path');
module.exports=async({page,check,load,text,calls,outDir,fixture,setSnapshot})=>{
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
};
