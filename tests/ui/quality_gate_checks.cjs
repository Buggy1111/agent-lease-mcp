/* Quality Gate v neonovém UI (B7 F1–F9 + review #56). Samostatný běh, jen syntetická data, žádné služby Agent Lease.
 *   python - <<<'...' vytáhne PAGE_HTML do souboru, pak:
 *   PLAYWRIGHT_MODULE=/cesta/k/playwright UI_PAGE_HTML=/tmp/page.html node tests/ui/quality_gate_checks.cjs
 */
const fs = require('node:fs');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html = fs.readFileSync(process.env.UI_PAGE_HTML || '/tmp/agent-lease-page.html', 'utf8');

const base = () => ({me: 'michal', paused: null, claims: [], mode: {mode: 'intelligent'},
  peers: [{agent: 'claude-code', active: true, presence: 'idle', seen_seconds_ago: 1}, {agent: 'codex', active: true, presence: 'idle', seen_seconds_ago: 1}],
  board: [], reviews: [], reviews_all: [], quality_runs: [], quality_requests: [],
  messages: [{id: 1, agent: 'codex', recipient: 'michal', kind: 'chat', text: 'TEST ONLY hello', seconds_ago: 5}]});
let snapshot = base();
const streams = new Set(), calls = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') { res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'}); res.end(html); }
  else if (url.pathname === '/api/snapshot') { res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify(snapshot)); }
  else if (url.pathname === '/api/events') { res.writeHead(200, {'Content-Type': 'text/event-stream'}); res.write(': ok\n\n'); streams.add(res); req.on('close', () => streams.delete(res)); }
  else if (url.pathname === '/api/research' || url.pathname === '/api/knowledge') { res.writeHead(200, {'Content-Type': 'application/json'}); res.end(url.pathname === '/api/research' ? '{"research":[]}' : '{"entries":[]}'); }
  else if (req.method === 'POST') { calls.push(url.pathname); res.writeHead(200, {'Content-Type': 'application/json'}); res.end('{}'); }
  else { res.writeHead(404); res.end(); }
});
const emit = data => { for (const s of streams) s.write(`event: room\ndata: ${JSON.stringify(data)}\n\n`); };
const wait = async (fn, label) => { for (let i = 0; i < 150; i++) { if (await fn()) return; await new Promise(r => setTimeout(r, 30)); } throw new Error('Timed out: ' + label); };
const results = [];
const check = async (name, fn) => { await fn(); results.push(name); console.log('PASS ' + name); };

(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1680, height: 1050}});
  const errors = [], external = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (!r.url().startsWith(origin)) external.push(r.url()); });
  const text = sel => page.locator(sel).textContent();
  const open = async s => { snapshot = s; await page.goto(origin + '/'); await page.waitForFunction(() => typeof qgRecords !== 'undefined' && document.querySelector('#feed .bubble')); await page.locator('.primary-nav [data-view="quality"]').click(); };
  try {
    await check('Empty Quality Gate never invents results', async () => {
      await open(base());
      assert.equal(await text('#qgRepoCount'), '(0)');
      assert((await text('#qgProjectTable')).includes('Žádný zaznamenaný běh'));
      assert((await text('#qgProjectDetail')).includes('Vyber kontrolu'));
    });
    await check('Recorded refusal replaces stale PASS and says checks did not start (B7, 9881233)', async () => {
      const refusal = {kind: 'quality_refusal', executed: false, run_id: 250, repository: 'fixture/refused', commit_sha: 'e'.repeat(40), agent: 'codex',
        overall_status: 'REVIEW', execution_status: 'not_started', error: '<img src=x onerror="window.injected=1">', checks: {tests: {status: 'NOT_RUN'}}};
      await open({...base(), quality_runs: [refusal]});
      assert.equal(await text('#qgRepoCount'), '(1)');
      assert.equal(await page.locator('[data-qg-run="250"]').count(), 1);
      assert((await text('#qgProjectTable')).includes('REVIEW') && !(await text('#qgProjectTable')).includes('PASS'));
      await page.locator('[data-qg-run="250"]').click();
      assert((await text('#qgProjectDetail')).includes('Kontroly nebyly spuštěny'));
      assert.equal(await page.locator('#qgProjectDetail img').count(), 0);
      emit({...base(), quality_runs: [{...refusal, kind: 'quality_attempt', overall_status: 'NOT_RUN'}]});
      await wait(() => page.evaluate(() => qgRecords?.[0]?.overall_status === 'NOT_RUN'), 'recorded optout');
      assert.equal(await page.evaluate(() => window.injected), undefined);
    });
    await check('Unexecuted runs without a run id are ignored', async () => {
      await open({...base(), quality_runs: [{kind: 'quality_run', executed: false, repository: 'fixture/ghost', overall_status: 'PASS'}, {kind: 'quality_refusal', repository: 'fixture/noid', overall_status: 'REVIEW'}]});
      assert.equal(await text('#qgRepoCount'), '(0)');
    });
    await check('Live record: requests incl. busy, safe log links only, selection follows run id', async () => {
      const checks = Object.fromEntries(['tests', 'lint', 'build', 'security', 'typecheck'].map(n => [n, {status: 'PASS', required: true}]));
      const live = {kind: 'quality_run', executed: true, run_id: 303, repository: 'fixture/live', commit_sha: 'c'.repeat(40), agent: 'codex', overall_status: 'REVIEW', checks, review_status: 'pending',
        logs: [{check: 'tests', url: '/api/quality-log?run_id=303&check=tests'}, {check: 'evil', url: 'javascript:window.injected=1'}]};
      await open({...base(), quality_runs: [live], quality_requests: [{repository: 'fixture/<img src=x onerror="window.injected=1">', state: 'busy', detail: 'TEST ONLY'}]});
      assert((await text('#qgRequests')).includes('čeká na volný runner'));
      assert.equal(await page.locator('#qgRequests img').count(), 0);
      await page.locator('[data-qg-run="303"]').click();
      assert.equal(await page.locator('#qgProjectDetail a').count(), 1);
      assert((await page.locator('#qgProjectDetail a').getAttribute('href')).startsWith('/api/quality-log?run_id=303&check=tests'));
      emit({...base(), quality_runs: [{...live, run_id: 304, repository: 'fixture/other'}, live]});
      await wait(async () => await text('#qgRepoCount') === '(2)', 'second repository');
      assert.equal(await text('#qgProjectTitle'), 'live');               // výběr zůstal na run 303, ne na první řádek
      assert.equal(await page.evaluate(() => window.injected), undefined);
    });
    await check('Read-only: no backend writes, no external requests, no page errors', async () => {
      assert.deepEqual(calls, []);
      assert.deepEqual(external, []);
      assert.deepEqual(errors, []);
    });
  } finally {
    await browser.close(); server.close();
  }
  console.log(`${results.length} PASS`);
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
