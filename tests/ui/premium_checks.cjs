/* Premium acceptance scenarios. All mutations target isolated TEST ONLY fixtures. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
module.exports = async function({page,check,load,fixture,emit,wait,text,getSnapshot,setSnapshot,outDir}) {
  const motionRecords = () => page.evaluate(()=>window.__motionRecords);
  const resetMotion = () => page.evaluate(()=>{window.__motionRecords=[];});
  const setFixture = async()=>{ const s=fixture(); s.peers.forEach(p=>p.seen_seconds_ago=30); setSnapshot(s); await load('/after'); };
  await page.setViewportSize({width:1920,height:1080});
  await page.emulateMedia({reducedMotion:'no-preference'});
  await setFixture();
  await check('Motion: history never enters; only new live messages enter for 210 ms',async()=>{
    assert.deepEqual(await motionRecords(),[]);
    const m={id:23,kind:'chat',agent:'codex',recipient:'michal',text:'TEST ONLY real SSE arrival',seconds_ago:0};
    emit('message',m,23);
    await wait(async()=>await page.locator('#msg-23').count()===1,'live arrival');
    let records=await motionRecords();
    assert.equal(records.filter(r=>r.id==='msg-23' && r.duration===210).length,1);
    const entry=records.find(r=>r.id==='msg-23'); assert.equal(entry.frames[0].transform,'translateY(6px)'); assert.equal(entry.frames[0].opacity,0);
    await resetMotion(); emit('message',m,23);
    emit('message',{...m,id:5,text:'TEST ONLY older replay'},5);
    await wait(async()=>await page.locator('#msg-5').count()===1,'older replay');
    assert.deepEqual(await motionRecords(),[]);
  });
  await check('Live status updates keep existing message text, agents, review controls and stop button',async()=>{
    await page.evaluate(()=>{ window.__kept={text:document.querySelector('#msg-11 .text'),agent:document.querySelector('[data-agent="codex"]'),review:document.querySelector('#reviews button'),stop:document.querySelector('#pause button')}; });
    const s=getSnapshot(); s.messages.find(m=>m.id===11).state='started'; s.board[1].active={id:11,text:'TEST ONLY started',from:'michal'}; emit('room',s);
    await wait(async()=>await text(page,'#metricRunning')==='2','in-place status update');
    assert(await page.evaluate(()=>window.__kept.text===document.querySelector('#msg-11 .text') && window.__kept.agent===document.querySelector('[data-agent="codex"]') && window.__kept.review===document.querySelector('#reviews button') && window.__kept.stop===document.querySelector('#pause button')));
    assert((await motionRecords()).some(r=>r.className.includes('task-state') && r.duration===180));
    assert((await motionRecords()).some(r=>r.id==='metricRunning' && r.duration===240));
  });
  await check('Completion is one 450 ms opacity overlay; errors never flash',async()=>{
    await resetMotion(); const s=getSnapshot(); s.messages.find(m=>m.id===10).state='succeeded'; emit('room',s);
    await wait(async()=>await text(page,'#metricDone')==='2','completed state');
    const flashes=(await motionRecords()).filter(r=>r.className==='completion-flash'); assert.equal(flashes.length,1); assert.equal(flashes[0].duration,450);
    assert.equal(flashes[0].frames[0].opacity,.12);
    await resetMotion(); s.messages.find(m=>m.id===11).state='failed'; emit('room',s);
    await wait(async()=>await page.locator('#msg-11 .task-state.bad').count()===1,'failure badge');
    assert.equal((await motionRecords()).filter(r=>r.className==='completion-flash' || r.className.includes('task-state')).length,0);
  });
  await check('Timeline tones reflect only actual task states; switching fades once',async()=>{
    const s=getSnapshot();
    for(const [id,tone] of [[13,'ok'],[12,'work'],[14,'warn'],[16,'bad'],[17,'muted']]) {
      await page.selectOption('#taskPicker',String(id));
      assert.equal(await page.locator('.timeline-current').getAttribute('data-tone'),tone);
    }
    await resetMotion(); await page.selectOption('#taskPicker','13');
    assert.equal((await motionRecords()).filter(r=>['taskDetail','taskTimeline'].includes(r.id) && r.duration===180).length,2);
    await resetMotion(); emit('room',s);
    await wait(async()=>await page.locator('#taskPicker').inputValue()==='13','selected detail');
    assert.equal((await motionRecords()).filter(r=>['taskDetail','taskTimeline'].includes(r.id)).length,0);
    assert.equal(await page.locator('#taskTimeline .event-list li').count(),2); // assignment + recorded reply, no invented phases
  });
  await check('Only active interactive agent dots pulse, offline and bridges remain still',async()=>{
    assert.equal(await page.locator('[data-agent="claude-code"] .dot').evaluate(el=>getComputedStyle(el).animationName),'online-presence');
    assert.equal(await page.locator('[data-agent="claude-code"] .dot').evaluate(el=>getComputedStyle(el).animationDuration),'2.4s');
    assert.equal(await page.locator('[data-agent="codex-bridge"] .dot').evaluate(el=>getComputedStyle(el).animationName),'none');
    assert((await text(page,'[data-agent="codex-bridge"]')).includes('vyčerpaný limit'));
    assert((await text(page,'[data-agent="claude-code"]')).includes('Aktivita'));
    const s=getSnapshot(); const ada=s.peers.find(p=>p.agent==='claude-code'); ada.active=false; ada.presence='offline'; emit('room',s);
    await wait(async()=>await page.locator('[data-agent="claude-code"].offline').count()===1,'offline transition');
    assert.equal(await page.locator('[data-agent="claude-code"] .dot').evaluate(el=>getComputedStyle(el).animationName),'none');
  });
  await check('Reduced motion cancels current animations and suppresses new effects',async()=>{
    await page.emulateMedia({reducedMotion:'reduce'}); await resetMotion();
    emit('message',{id:30,kind:'chat',agent:'codex',text:'TEST ONLY reduced motion',seconds_ago:0},30);
    await wait(async()=>await page.locator('#msg-30').count()===1,'reduced motion new message');
    assert.deepEqual(await motionRecords(),[]);
    assert.equal(await page.evaluate(()=>document.getAnimations().length),0);
    assert.equal(await page.locator('#sendBtn').evaluate(el=>getComputedStyle(el).transitionDuration),'0s');
  });
  await check('Tablet drawer traps keyboard focus, closes with Escape, and retains the one emergency control',async()=>{
    await setFixture(); await page.setViewportSize({width:768,height:1024});
    assert.equal(await page.locator('#side').isVisible(),false); assert.equal(await page.locator('#inspector').isVisible(),false);
    await page.focus('#openAgents'); await page.keyboard.press('Enter');
    await page.waitForFunction(()=>document.getElementById('workspaceDrawer').open);
    assert(await page.locator('#workspaceDrawer #side').isVisible());
    assert.equal(await page.locator('#pause').count(),1);
    assert(await page.locator('#workspaceDrawer #pause button').isEnabled());
    for(let i=0;i<10;i++) { await page.keyboard.press('Tab'); assert(await page.locator('#workspaceDrawer').evaluate(el=>el.contains(document.activeElement))); }
    await page.keyboard.press('Escape');
    await wait(async()=>await page.locator('#pause').evaluate(el=>el.parentElement.id==='masthead'),'restore emergency control');
    assert.equal(await page.locator('#openAgents').evaluate(el=>el===document.activeElement),true);
    await page.click('#openDetail'); await page.selectOption('#taskPicker','16');
    assert.equal(await page.locator('.timeline-current').getAttribute('data-tone'),'bad');
    await page.getByRole('button',{name:'Zavřít panel'}).click();
    await wait(async()=>await page.locator('#workspaceDrawer').evaluate(el=>!el.open),'drawer close');
  });
  await check('Desktop/notebook/tablet/mobile breakpoints and 44 px mobile targets',async()=>{
    for(const [width,height] of [[1920,1080],[1366,768],[1280,900],[900,900],[899,900],[768,1024],[600,900],[599,844],[390,844],[320,844]]) {
      await page.setViewportSize({width,height});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'overflow '+width);
      assert.equal(await page.locator('#inspector').isVisible(),width>1280,'inspector '+width);
      assert.equal(await page.locator('#side').isVisible(),width>=900,'sidebar '+width);
      if(width<600) {
        const small=await page.evaluate(()=>[...document.querySelectorAll('button,select,input[type="search"],summary,.mobile-nav a,.system-toggle')].filter(el=>el.getClientRects().length && getComputedStyle(el).visibility!=='hidden').map(el=>({name:el.textContent.trim().slice(0,40)||el.id,rect:el.getBoundingClientRect()})).filter(x=>x.rect.width<43.9||x.rect.height<43.9).map(x=>({name:x.name,width:x.rect.width,height:x.rect.height})));
        assert.deepEqual(small,[],'small targets at '+width);
      }
    }
  });
  await check('200% zoom reflows without page-wide horizontal scrolling or inaccessible stop control',async()=>{
    await page.setViewportSize({width:1920,height:1080});
    await page.evaluate(()=>document.documentElement.style.zoom='2');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'200% CSS zoom overflow');
    assert(await page.getByRole('button',{name:'Zastavit všechny agenty'}).isVisible());
    await page.evaluate(()=>document.documentElement.style.zoom='');
  });
  await check('Palette text and control surfaces meet 4.5:1 including primary button hover',async()=>{
    const report=await page.evaluate(()=>{
      const canvas=document.createElement('canvas'); canvas.width=canvas.height=1; const ctx=canvas.getContext('2d',{willReadFrequently:true});
      const rgb=value=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=value;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data].slice(0,3);};
      const lum=c=>c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
      const ratio=(a,b)=>{const x=lum(rgb(a)),y=lum(rgb(b));return(Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
      const probe=document.createElement('span'); document.body.append(probe);
      const resolve=value=>{probe.style.color=value;return getComputedStyle(probe).color;};
      const pairs=[];
      for(const fg of ['--text','--text-dim','--accent-text','--blue-text','--ok','--warn','--bad-text']) for(const bg of ['--bg','--surface','--surface-2']) pairs.push({label:fg+' / '+bg,ratio:ratio(resolve('var('+fg+')'),resolve('var('+bg+')'))});
      for(const amount of [78,82,88,91]) pairs.push({label:'primary '+amount+'%',ratio:ratio(resolve('var(--text)'),resolve('color-mix(in srgb,var(--accent-1) '+amount+'%,var(--bg))'))});
      probe.remove(); return pairs;
    });
    fs.writeFileSync(path.join(outDir,'contrast-results.json'),JSON.stringify(report,null,2));
    assert.deepEqual(report.filter(p=>p.ratio<4.5),[]);
  });
};
