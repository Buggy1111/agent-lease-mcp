/* Frontend integration checks. Fixture HTTP/SSE only; never connects to Agent Lease services.
 * PLAYWRIGHT_MODULE points to an installed playwright/playwright-core module.
 * Input HTML is extracted from PAGE_HTML, not a parallel frontend implementation.
 */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const beforeHTML = fs.readFileSync(process.env.UI_BEFORE_HTML || '/tmp/agent-lease-ui-before.html', 'utf8');
const afterHTML = fs.readFileSync(process.env.UI_AFTER_HTML || '/tmp/agent-lease-ui-after.html', 'utf8');
const observed = JSON.parse(fs.readFileSync(process.env.UI_SNAPSHOT || 'docs/ui-redesign/observed-snapshot.json', 'utf8'));
const outDir = process.env.UI_OUTPUT || 'docs/ui-redesign';
fs.mkdirSync(outDir, {recursive:true});
let snapshot = structuredClone(observed);
const streams = new Set();
const calls = [];
const checks = [];
const errors = [];
let failSend = false;
const server = http.createServer((req,res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/before' || url.pathname === '/after') {
    res.writeHead(200, {'Content-Type':'text/html; charset=utf-8', 'Content-Security-Policy':"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"});
    res.end(url.pathname === '/before' ? beforeHTML : afterHTML);
  } else if (url.pathname === '/api/snapshot') {
    res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(snapshot));
  } else if (url.pathname === '/api/events') {
    res.writeHead(200, {'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});
    res.write(': ready\n\n'); streams.add(res); req.on('close',()=>streams.delete(res));
  } else if (req.method === 'POST') {
    let raw=''; req.on('data',d=>raw+=d); req.on('end',()=>{
      const body = raw ? JSON.parse(raw) : {};
      calls.push({url:url.pathname,body});
      if (url.pathname === '/api/pause') snapshot.paused = {by:'michal',reason:body.reason};
      if (url.pathname === '/api/resume') snapshot.paused = null;
      res.writeHead(failSend && url.pathname === '/api/send' ? 503 : 200, {'Content-Type':'application/json'});
      res.end(failSend && url.pathname === '/api/send' ? 'fixture: send failed' : JSON.stringify({id:9000,paused:!!snapshot.paused}));
    });
  } else { res.writeHead(404); res.end(); }
});
const emit = (event,data,id) => { for (const s of streams) s.write(`event: ${event}\n${id ? `id: ${id}\n` : ''}data: ${JSON.stringify(data)}\n\n`); };
const wait = async (fn, label) => { for(let i=0;i<100;i++){ if(await fn()) return; await new Promise(r=>setTimeout(r,30)); } throw new Error('Timed out: '+label); };
const text = (page,selector) => page.locator(selector).textContent();
const check = async(name,fn) => { await fn(); checks.push(name); console.log('PASS '+name); };
async function noOverflow(page,width) {
  await page.setViewportSize({width,height:900});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth), `horizontal overflow at ${width}`);
}
function fixture() {
  // Synthetic test-only records. These are never used for the comparison screenshots.
  const task=(id,state,extra={})=>({id,kind:'task',agent:'michal',recipient:'codex',text:`TEST ONLY task ${id}`,state,attempts:1,seconds_ago:300-id,seen:true,accepted:false,accept_note:'',last_error:'',reply_to:null,...extra});
  return {me:'michal',paused:null,claims:[{path:'/tmp/ui-test/long-path',held_by:'codex',purpose:'TEST ONLY',expires_in_seconds:900}],
    peers:[{agent:'claude-code',active:true,presence:'busy',status:'TEST ONLY review'},{agent:'codex',active:true,presence:'busy',status:'TEST ONLY work'}, {agent:'codex-bridge',active:true,presence:'idle',status:'TEST ONLY bridge',provider_state:'limited',provider_resets_at:1793990000},{agent:'claude-code-bridge',active:true,presence:'idle'},{agent:'michal',active:true,presence:'idle'},{agent:'offline-test',active:false,presence:'offline'},{agent:'untrusted-test',active:true,presence:'busy'}],
    board:[{agent:'codex',queued:99,unaccepted:80,needs_review:99,presence:'busy',active:{id:10,text:'TEST ONLY task',from:'michal'}}, {agent:'claude-code',queued:7,unaccepted:3,needs_review:5,active:null}],
    reviews:[{id:7,topic:'TEST ONLY <img src=x onerror="window.injected=1">',repo:'/tmp/ui-test',author:'codex',reviewer:'claude-code',head_commit:'a'.repeat(40),state:'escalated',fix_cycles:3,max_fix_cycles:3,cycles_left:0,verdict_note:'TEST ONLY changes requested'}],
    messages:[task(10,'started',{text:'TEST ONLY long task\n'+('Line of task data\n'.repeat(60)),accepted:true,accept_note:'TEST ONLY taking'}),task(11,'pending'),task(12,'leased'),task(13,'succeeded'),task(14,'needs_review',{last_error:'quarantine: TEST ONLY'}),task(15,'needs_review',{last_error:'checkpoint: TEST ONLY changed workspace'}),task(16,'failed'),task(17,'cancelled'),{id:20,agent:'michal',recipient:'codex',kind:'chat',text:'TEST ONLY message <img src=x onerror="window.injected=1">',seconds_ago:20},{id:21,agent:'codex',recipient:'michal',kind:'result',reply_to:13,text:'TEST ONLY: 12 passed, review pending',seconds_ago:10},{id:22,agent:'broker',recipient:'codex',kind:'result',reply_to:10,text:'TEST ONLY recorded event',seconds_ago:5}]};
}
(async()=>{
  let browser;
  try {
    await new Promise(r=>server.listen(0,'127.0.0.1',r));
    const origin=`http://127.0.0.1:${server.address().port}`;
    browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {}),args:['--no-sandbox']});
    const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'cs-CZ',timezoneId:'Europe/Prague',colorScheme:'dark',reducedMotion:'reduce'});
    const page=await context.newPage();
    await page.addInitScript(()=>{
      window.__motionRecords=[];
      const animate=Element.prototype.animate;
      Element.prototype.animate=function(frames,options){window.__motionRecords.push({id:this.id,className:typeof this.className==='string'?this.className:'svg',frames,duration:typeof options==='number'?options:options.duration});return animate.call(this,frames,options);};
    });
    page.on('pageerror',e=>errors.push(e.message));
    const external=[];
    page.on('request',r=>{if(!r.url().startsWith(origin)) external.push(r.url());});
    const load=async(route)=>{ await page.goto(origin+route); await page.waitForFunction(()=>document.getElementById('statusText')?.textContent==='živě'); };
    await check('Comparison screenshots use the same observed Agent Lease records',async()=>{
      await load('/before');
      await page.screenshot({path:path.join(outDir,'before-desktop.png')});
      await page.setViewportSize({width:1339,height:758});
      await page.screenshot({path:path.join(outDir,'before-reference-size.png')});
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(outDir,'before-mobile.png')});
      await page.screenshot({path:path.join(outDir,'before-mobile-full.png'),fullPage:true});
      await page.setViewportSize({width:1440,height:1000});
      await load('/after');
      const task = observed.messages.filter(m=>m.kind==='task').at(-1);
      if(task) await page.selectOption('#taskPicker',String(task.id));
      await page.screenshot({path:path.join(outDir,'after-desktop.png')});
      await page.setViewportSize({width:1339,height:758});
      await page.screenshot({path:path.join(outDir,'after-reference-size.png')});
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(outDir,'after-mobile.png')});
      await page.screenshot({path:path.join(outDir,'after-mobile-full.png'),fullPage:true});
      await noOverflow(page,1024);
      await page.screenshot({path:path.join(outDir,'after-tablet.png'),fullPage:true});
      await noOverflow(page,1280);
      await page.screenshot({path:path.join(outDir,'after-laptop.png')});
      for(const width of [320,375,390,768,1024,1280,1440,1920]) await noOverflow(page,width);
      if(process.env.UI_PREMIUM==='1') {
        for(const [name,width,height] of [['desktop-1920',1920,1080],['laptop-1366',1366,768],['tablet-768',768,1024],['mobile-390',390,844]]) {
          await page.setViewportSize({width,height}); await page.screenshot({path:path.join(outDir,'after-'+name+'.png')});
        }
        await page.setViewportSize({width:768,height:1024}); await page.click('#openDetail');
        await page.screenshot({path:path.join(outDir,'after-tablet-detail.png')}); await page.getByRole('button',{name:'Zavřít panel'}).click();
        await page.setViewportSize({width:390,height:844}); await page.click('#openAgents');
        await page.screenshot({path:path.join(outDir,'after-mobile-agents.png')}); await page.getByRole('button',{name:'Zavřít panel'}).click();
        await page.setViewportSize({width:1920,height:1080});
        await page.locator('.primary-nav [data-view="quality"]').click();
        await page.screenshot({path:path.join(outDir,'quality-gate-empty.png')});
        await page.locator('#qualityPage [data-qg-demo]').click();
        await page.locator('#qgProjectTable [data-qg-project="0"]').click();
        await page.screenshot({path:path.join(outDir,'quality-gate-demo.png')});
        await page.setViewportSize({width:390,height:844});
        await page.screenshot({path:path.join(outDir,'quality-gate-mobile.png')});
        await page.locator('#qualityPage [data-qg-demo]').click();
        await page.locator('.primary-nav [data-view="overview"]').click();
        await page.setViewportSize({width:1920,height:1080});
      }
    });
    snapshot=fixture();
    await load('/after');
    await check('Task metrics use the whole board beyond history; bridges/operators excluded from agents',async()=>{
      assert.equal(await text(page,'#metricAgents'),'2'); assert.equal(await text(page,'#metricRunning'),'1');
      assert.equal(await text(page,'#metricQueued'),'106'); assert.equal(await text(page,'#metricDone'),'1');
      assert.equal(await text(page,'#metricReview'),'104'); assert.equal(await text(page,'#metricSystem'),'Provoz povolen');
      assert.equal(await page.locator('.card.bridge').count(),2);
      assert((await text(page,'#projects')).includes('/tmp/ui-test'));
      assert.equal(await page.locator('#agents').getByText('untrusted-test',{exact:true}).count(),0);
    });
    await check('Reference navigation opens actual sections and task filter',async()=>{
      await page.click('#navTasks'); assert.equal(await page.locator('[data-filter="task"]').getAttribute('aria-pressed'),'true');
      await page.click('[data-filter="all"]');
      assert.equal(await page.locator('#navAgents').count(),1);
      assert.equal(await page.locator('#navProjects').count(),1);
      assert.equal(await page.locator('#navHistory').count(),1);
    });
    await check('Kind filters, system events, search, keyboard focus',async()=>{
      await page.click('[data-filter="task"]'); assert.equal(await page.locator('.bubble:visible').count(),8);
      await page.fill('#search','#11'); assert.equal(await page.locator('.bubble:visible').count(),1);
      await page.fill('#search',''); await page.click('[data-filter="result"]'); assert.equal(await page.locator('.bubble:visible').count(),1);
      await page.click('[data-filter="system"]'); assert.equal(await page.locator('.bubble:visible').count(),1);
      await page.click('[data-filter="all"]'); await page.check('#showSystem'); assert.equal(await page.locator('.bubble:visible').count(),11);
      await page.uncheck('#showSystem');
      await page.locator('#search').focus(); assert(await page.locator('#search').evaluate(el=>el===document.activeElement));
    });
    await check('Untrusted message/review text is escaped, never executed',async()=>{
      assert.equal(await page.evaluate(()=>window.injected),undefined);
      assert.equal(await page.locator('#msg-20 img, #reviews img').count(),0);
      assert((await text(page,'#msg-20')).includes('<img'));
    });
    await check('Detail shows only the selected task and actual linked records',async()=>{
      await page.selectOption('#taskPicker','13');
      assert((await text(page,'#taskTimeline')).includes('12 passed'));
      assert(!(await text(page,'#taskTimeline')).includes('TEST ONLY recorded event'));
      await page.click('#msg-11 .detail');
      assert.equal(await text(page,'#detailTitle'),'Detail úkolu #11');
      assert((await text(page,'#taskTimeline')).includes('Žádné navázané zprávy'));
      assert.equal(await page.locator('#detailTitle').evaluate(el=>el===document.activeElement),true);
    });
    await check('SSE updates state without duplicate messages, preserves expanded text',async()=>{
      await page.click('#msg-10 .more');
      snapshot.messages.find(m=>m.id===11).state='started';
      snapshot.board[1].active={id:11,text:'TEST ONLY started',from:'michal'};
      await page.locator('#taskDetail .task-description summary').click();
      emit('room',snapshot);
      await wait(async()=>await text(page,'#metricRunning')==='2','SSE running counter');
      assert.equal(await page.locator('#msg-11').count(),1);
      assert.equal(await page.locator('#msg-10 .text.clip').count(),0);
      assert((await text(page,'#taskDetail')).includes('pracuje se'));
      assert(await page.locator('#taskDetail .task-description').evaluate(el=>el.open));
      emit('room',snapshot);
      assert(await page.locator('#taskDetail .task-description').evaluate(el=>el.open));
      emit('message',snapshot.messages.find(m=>m.id===11),11);
      await wait(async()=>await page.locator('#msg-11').count()===1,'dedup');
    });
    page.on('dialog',async d=>{ await d.accept(d.type()==='prompt' ? 'TEST ONLY reason' : undefined); });
    await check('Existing cancel, retry, quarantine, checkpoint and review actions keep payloads',async()=>{
      await page.locator('#msg-11 button').filter({hasText:'Zrušit úkol'}).click();
      await wait(()=>calls.some(c=>c.url==='/api/cancel' && c.body.id===11 && c.body.to==='codex'),'cancel');
      await page.locator('#msg-14 button').filter({hasText:'Zkusit znovu'}).click();
      await page.locator('#msg-14 button').filter({hasText:'Schválit a spustit jednou'}).click();
      await page.locator('#msg-15 button').filter({hasText:'Přesto navázat'}).click();
      await page.getByRole('button',{name:'Povolit ještě 1 kolo'}).click();
      await page.getByRole('button',{name:'Uzavřít review'}).click();
      await wait(()=>calls.some(c=>c.url==='/api/review-close'),'review close');
      for(const url of ['/api/retry','/api/approve','/api/approve-resume','/api/review-grant','/api/review-close']) assert(calls.some(c=>c.url===url),url);
      for(const url of ['/api/approve','/api/approve-resume','/api/review-grant','/api/review-close']) assert.equal(calls.find(c=>c.url===url).body.reason,'TEST ONLY reason');
    });
    await check('Composer preserves message/task/broadcast payloads and Enter/Shift+Enter',async()=>{
      await page.selectOption('#to','claude-code'); await page.fill('#text','TEST ONLY chat'); await page.press('#text','Enter');
      await wait(()=>calls.some(c=>c.url==='/api/send'),'send chat');
      assert.deepEqual(calls.filter(c=>c.url==='/api/send').at(-1).body,{text:'TEST ONLY chat',kind:'chat',to:'claude-code'});
      await page.click('[data-kind="task"]'); assert(await page.locator('#to option[value="*"]').evaluate(o=>o.disabled));
      await page.fill('#text','TEST ONLY task'); await page.press('#text','Shift+Enter'); assert((await page.inputValue('#text')).includes('\n'));
      await page.click('#sendBtn'); await wait(()=>calls.filter(c=>c.url==='/api/send').length===2,'send task');
      assert.equal(calls.filter(c=>c.url==='/api/send').at(-1).body.kind,'task');
      await wait(async()=>await page.inputValue('#text')==='','composer reset');
      await page.selectOption('#to','*'); await page.fill('#text','TEST ONLY all'); await page.click('#sendBtn');
      await wait(()=>calls.filter(c=>c.url==='/api/send').length===3,'broadcast');
      assert.deepEqual(calls.filter(c=>c.url==='/api/send').at(-1).body,{text:'TEST ONLY all',kind:'broadcast',to:''});
    });
    await check('Failed sends preserve the composer text and re-enable retry',async()=>{
      failSend=true;
      await page.selectOption('#to','codex'); await page.fill('#text','TEST ONLY retained draft');
      await page.click('#sendBtn');
      await wait(async()=>!(await page.locator('#sendBtn').isDisabled()),'failed send unlock');
      assert.equal(await page.inputValue('#text'),'TEST ONLY retained draft');
      failSend=false;
    });
    await check('Emergency stop/resume, missing pause state and stream reconnect',async()=>{
      await page.getByRole('button',{name:'Zastavit všechny agenty'}).click();
      await page.getByRole('button',{name:'Pustit agenty znovu'}).waitFor();
      const withoutPause={...snapshot}; delete withoutPause.paused; emit('room',withoutPause);
      assert.equal(await page.getByRole('button',{name:'Pustit agenty znovu'}).count(),1);
      await page.getByRole('button',{name:'Pustit agenty znovu'}).click();
      await page.getByRole('button',{name:'Zastavit všechny agenty'}).waitFor();
      for(const stream of [...streams]) stream.end();
      await page.waitForFunction(()=>document.getElementById('statusText').textContent.includes('přerušeno'));
      assert.equal(await text(page,'#metricSystem'),'Spojení přerušeno');
      await page.waitForFunction(()=>document.getElementById('statusText').textContent==='živě',{},{timeout:15000});
      emit('room',snapshot);
    });
    await check('History is capped at 200 unique messages; missing data stays explicit',async()=>{
      snapshot.messages=Array.from({length:205},(_,i)=>({id:100+i,agent:'codex',kind:'chat',text:'TEST ONLY history '+i,seconds_ago:205-i}));
      emit('room',snapshot);
      await wait(async()=>await page.locator('#feed .bubble').count()===200,'bounded history');
      assert.equal(await text(page,'#metricRunning'),'2');
      assert.equal(await text(page,'#metricQueued'),'106');
      assert.equal(await text(page,'#metricReview'),'104');
      assert((await text(page,'#taskDetail')).includes('už není v načtené historii'));
      snapshot={me:'michal',peers:[],messages:[],claims:[],board:[],reviews:[],paused:null};
      await load('/after');
      assert.equal(await text(page,'#metricAgents'),'0'); assert.equal(await text(page,'#metricDone'),'0');
      assert((await text(page,'#filterEmpty')).includes('zatím nemá'));
    });
    await check('Board-only SSE updates work without messages; absent board stays unknown',async()=>{
      snapshot.board=[{agent:'codex',queued:26,unaccepted:20,needs_review:5,active:{id:1,text:'TEST ONLY leased outside history',from:'michal'}}];
      emit('room',snapshot);
      await wait(async()=>await text(page,'#metricQueued')==='26','authoritative queue');
      assert.equal(await text(page,'#metricReview'),'5');
      assert.equal(await text(page,'#metricRunning'),'1');
      assert.equal(await text(page,'#metricDone'),'0');
      delete snapshot.board; await load('/after');
      for(const id of ['metricRunning','metricQueued','metricReview']) assert.equal(await text(page,'#'+id),'—');
      snapshot.board=[]; emit('room',snapshot);
      await wait(async()=>await text(page,'#metricQueued')==='0','known empty board');
      assert.equal(await text(page,'#metricRunning'),'0'); assert.equal(await text(page,'#metricReview'),'0');
    });
    if(process.env.UI_PREMIUM==='1') await require('./quality_gate_checks.cjs')({page,check,load,text,calls,outDir,fixture,setSnapshot:s=>{snapshot=s;}});
    if(process.env.UI_PREMIUM==='1') await require('./premium_checks.cjs')({page,check,load,fixture,emit,wait,text,getSnapshot:()=>snapshot,setSnapshot:s=>{snapshot=s;},outDir});
    await check('No script errors, external requests or horizontal overflow',async()=>{
      assert.deepEqual(errors,[]); assert.deepEqual(external,[]);
      for(const width of [320,390,768,1024,1280,1440,1920]) await noOverflow(page,width);
      assert.equal(await page.locator('button').first().evaluate(el=>getComputedStyle(el).transitionDuration),'0s');
    });
    fs.writeFileSync(path.join(outDir,'browser-results.json'),JSON.stringify({checks,errors,externalRequests:external,viewports:[320,375,390,768,1024,1280,1440,1920],screenshotData:'Actual observed agent-lease snapshot; synthetic fixture used only for behavior assertions'},null,2));
    console.log(`${checks.length} browser checks passed`);
  } finally {
    if(browser) await browser.close();
    for(const s of streams) s.end();
    server.closeAllConnections(); await new Promise(r=>server.close(r));
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
