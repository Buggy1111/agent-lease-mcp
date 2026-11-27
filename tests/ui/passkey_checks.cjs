/* Windows Hello (WebAuthn) ve skutečném Chromiu s virtuálním autentizátorem:
 * přidání klíče, odhlášení → přihlašovací stránka, přihlášení PINem, bez PINu odmítnuto.
 * node tests/ui/passkey_checks.cjs http://localhost:<port>/ <jednorázový kód>
 */
const [, , base, code] = process.argv;
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  const cdp = await ctx.newCDPSession(p);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true, isUserVerified: true } });
  const loggedIn = () => p.waitForFunction(() => document.querySelectorAll('#feed .bubble').length > 3 && document.getElementById('loginScreen').hidden, null, { timeout: 20000 });
  const logout = async () => { await ctx.clearCookies(); await p.evaluate(() => localStorage.clear()); await p.goto(base); await p.waitForSelector('#loginScreen:not([hidden])', { timeout: 15000 }); };
  try {
    await p.goto(base + '#token=' + code);
    await loggedIn();
    await p.click('.primary-nav [data-view="settings"]');
    await p.click('#pkAdd');
    await p.waitForFunction(() => document.getElementById('pkStatus').textContent.includes('Hotovo'), null, { timeout: 15000 });
    await p.waitForFunction(() => document.getElementById('pkList').textContent.includes('Windows Hello'), null, { timeout: 10000 });
    await logout();
    await p.click('#loginHello');
    await loggedIn();
    await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: false });
    await logout();
    await p.click('#loginHello');
    await p.waitForFunction(() => /zrušeno|nepovedlo/.test(document.getElementById('loginStatus').textContent), null, { timeout: 15000 });
    if (!(await p.locator('#loginScreen').isVisible())) throw new Error('logged in without PIN');
    if (errs.length) throw new Error(errs.join('; '));
    console.log('PASSKEY OK');
  } finally {
    await b.close();
  }
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
