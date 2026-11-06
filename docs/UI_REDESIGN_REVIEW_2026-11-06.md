# Agent Lease UI — podklady k review

Redesign vychází z Michalovy vizualizace (1339 × 758 px) a zachovává existující HTTP API i backend. Je připravený v izolované větvi `nox/ui-redesign-20261106` nad `d01bf3e`, v `~/dev/work/agent-lease-ui-redesign`. Není sloučený do masteru, pushnutý ani nasazený.

## Výsledek

- Kompaktní dark dashboard s trojúhelníkovou značkou, horní navigací a modrými/fialovými/zelenými akcenty podle reference. Podsvícení je statické; nejsou trvalé animace, externí fonty ani CDN.
- Šest horních panelů: aktivní skuteční agenti (bez mostů/operátora), běžící úkoly (`started`), čekající (`pending`), dokončené (`succeeded`), ke kontrole (`needs_review`) a skutečný stav pauzy/spojení. Přidělené `leased` se v detailu stále liší od zahájených. Počty úkolů se vztahují pouze k načtené historii nejvýše 200 posledních zpráv a tento rozsah je uvedený na obrazovce.
- Vlevo odlišení agenta, mostu a operátora, jejich původní stav, limity, práce/fronta a blokace. Offline seznam i claims jsou zachovány; Ada a Nox zůstávají v hlavním seznamu i offline. Dlouhý stav se zobrazí stručně, celý text je v tooltipu. Fialově se zvýrazní příjemce vybraného úkolu.
- Uprostřed zachovaná místnost se stavy, příjemkami, celým textem, původními akcemi úkolů a composerem. Nové filtry Vše/Úkoly/Výsledky/Systém a hledání podle textu, jména či `#ID`. Navigace vede na skutečně existující části.
- Vpravo samostatné záznamy vybraného úkolu, jeho detail, review commitů a poslední zaznamenané události. Časová osa obsahuje zadání a přímo navázané zprávy; nenaznačuje hypotetické fáze nebo procenta dokončení. Zadání a dlouhé výsledky jsou rozbalovací.
- Mobil upřednostní místnost a nabízí odkazy na agenty/detail. Nouzové zastavení/obnovení je v horní sticky liště. Otevřené zprávy a detail zůstávají otevřené přes SSE aktualizace; neúspěšné odeslání zachová rozepsaný text.

## Screenshoty

Všechny snímky používají stejný skutečný snímek z agent-lease, získaný během auditu: 51 zpráv od #651 do #701, pět účastníků a existující claim izolovaného worktree. `reviews: []` bylo ověřeno nástrojem review/list. Pro tento replay nejsou k dispozici údaje `board`; stav agenta se proto na snímcích bere z původního `peers.status`. Backend v reálném UI nadále posílá board v nezměněném kontraktu. Testovací, smyšlená data použitá pro funkční kontroly se na srovnávacích screenshotech nepoužívají. Relativní časy jsou hodnoty původního snímku, nikoli aktuální provozní přehled.

| Rozměr | Před | Po |
| --- | --- | --- |
| Shodný s předlohou, 1339 × 758 | [Před](ui-redesign/before-reference-size.png) | [Po](ui-redesign/after-reference-size.png) |
| PC, 1440 × 1000 | [Před](ui-redesign/before-desktop.png) | [Po](ui-redesign/after-desktop.png) |
| Mobil, 390 × 844 | [Před](ui-redesign/before-mobile.png) | [Po](ui-redesign/after-mobile.png) |
| Celá mobilní stránka | [Před](ui-redesign/before-mobile-full.png) | [Po](ui-redesign/after-mobile-full.png) |

Dále [notebook 1280 px](ui-redesign/after-laptop.png) a [tablet 1024 px](ui-redesign/after-tablet.png). Prohlížeč: lokální Chromium, locale cs-CZ, dark, reduced-motion. Snímky nevyžadovaly přihlášení do provozního serveru ani čtení jeho tokenu/databáze.

## Rozsah diffu a bezpečnost

Produkční změna je pouze uvnitř `PAGE_HTML` v `src/agent_lease_mcp/webui.py`. Skript `tests/ui/prepare_pages.py` ověřuje bajtovou shodu Python kódu před a za touto konstantou vůči základnímu commitu. Nemění se handlery, SQLite, ownership, leases, edit gate, karanténa, checkpoint ověřování, emergency stop, oprávnění, bridge, probouzení, služby ani API kontrakty. CSS a JS jsou nadále inline kvůli původnímu CSP.

[Inventura před změnami](UI_REDESIGN_AUDIT_2026-11-06.md) dokumentuje zachované funkce a datová omezení. [Frontendový diff](ui-redesign/frontend.patch) ukazuje změnu samotné konstanty; další soubory jsou pouze testy, dokumentace a lokální důkazy.

## Ověření

- Plná Python regrese: první průchod **529 passed, 1 xfailed**, 203,52 s. Druhý plný průchod: **529 passed, 1 xfailed**, 186,25 s. Po posledním doladění HTML/CSS ještě cíleně **23 webových testů PASS** (10,25 s) a znovu všech 13 browser scénářů PASS.
- Ruff pro změněný Python a extrakční skript; `node --check`; `git diff --check`.
- Automatizovaný browser test: **13 scénářů PASS**; čítače z konkrétních stavů a bez mostů; filtry/hledání/systémové zprávy; původní payloady send/cancel/retry/approve/approve-resume/review-grant/review-close; Enter/Shift+Enter/broadcast; skutečný vztah `reply_to`; SSE změna stavu, deduplikace, otevřený detail a reconnect; pauza/obnovení včetně `paused === undefined`; zachování draftu při chybě; limit 200 záznamů, prázdná historie, escapování HTML, reduced-motion, žádné externí požadavky nebo JS chyby.
- Ověřené šířky: **320, 375, 390, 768, 1024, 1280, 1440, 1920 px**, bez vodorovného přesahu. [Strojový browser report](ui-redesign/browser-results.json).
- HTTP/SSE fixture testů poslouchá jen na náhodném portu loopbacku. Testy se nepřipojují na skutečný server. Testovací scénáře jsou výslovně označené `TEST ONLY`.

### Opakování kontrol

Použij projektový Python s pytest/Ruff a existující instalaci Playwright nebo playwright-core s Chromium. Produkční závislosti se nemění.

```bash
python3 tests/ui/prepare_pages.py --base d01bf3e
node --check /tmp/agent-lease-ui-script.js
PLAYWRIGHT_MODULE=/cesta/k/playwright-core CHROMIUM_PATH=/cesta/k/chrome \
  node tests/ui/browser_check.cjs
python3 -m pytest -q
ruff check src/agent_lease_mcp/webui.py tests/ui/prepare_pages.py
git diff --check
```

`UI_OUTPUT` může přesměrovat screenshoty a JSON report do jiného adresáře, `UI_SNAPSHOT` vstupní snímek. Testovací prohlížeč/server potřebují oprávnění pro lokální sockety (omezený sandbox je blokuje).

## Omezení a zaměření review Ady

1. Graf výkonu, úspěšnost, analytics, nastavení a přílohy z vizualizace nemají podklad v existující aplikaci; nepřidávají se nefunkční tlačítka ani falešné statistiky. Samostatný seznam projektů není dostupný: zobrazují se jen explicitní repozitáře z review. Při prázdných datech je to uvedeno.
2. Chybí API pro úplný audit úkolu, strukturované checkpoint kroky a testové výsledky. Timeline proto zobrazuje pouze původní zadání a přímo navázané zprávy z načtené historie; commit review se nespojuje s úkolem odhadem.
3. Přehled spojení a pauzy není health check procesů. Chyba spojení označí stav jako přerušený; poslední známé ovládání pauzy zůstává k dispozici.
4. Automatické vizuální/behaviorální kontroly proběhly v Chromiu; Firefox/Safari ani fyzická dotyková zařízení nejsou ověřena. Snímek je replay, nikoli živý zátěžový test produkce.
5. Review má ověřit zachování všech stávajících akcí, pravdivost datových rozsahů, escapování textů a použitelnost návrhu. Nasazení a merge zůstávají zvlášť na Michalově schválení. Fáze A/A11 a její služby nebyly změněny.
