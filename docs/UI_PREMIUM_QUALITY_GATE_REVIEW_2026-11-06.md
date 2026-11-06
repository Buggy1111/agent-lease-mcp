# Premium UI + Quality Gate — podklady k review

Izolovaná větev `nox/ui-redesign-20261106`, navazující na `d8e5795`, zachovává původní backend z d01bf3e. Neproběhl push, merge, deploy ani zásah do běžících služeb. Automatické observační ukládání claude-mem hlásilo limit; podklady jsou uloženy jako soubory a stav se zapisuje přímo do work state.

## Změny

- Centrální paleta podle specifikace (#070B14, #0D1423, #131D30, #9146FF, #38A8FF, #25DFA0, #F5B94F, #FF5978, #F0F4FF, #94A6C2). Ostatní povrchy a barvy jsou odvozené CSS proměnnými/color-mix. Dřívější vrstvené přepisy CSS byly sjednoceny.
- Panely 12–16 px, jemné statické gradienty, okraje a stíny; hover 200 ms s předepsanou easing křivkou, posun pouze u KPI karet. Žádné velkoplošné blur animace, framework nebo externí fonty.
- KPI: skutečná hodnota se přepíše přímo, poté krátké zvýraznění 240 ms. Žádný interpolovaný/falešný počet. Online bod 2,4 s a opacity 0,65–1 pouze u aktivního interaktivního agenta, nikoli bridge/offline. U agentů se zobrazí dostupná poslední aktivita.
- Nová živá zpráva vstupuje 210 ms opacity/translateY 6 px; historie, replay a změna stavu neanimují vstup. Stavový štítek se mění lokálně, dokončení má jeden zelený opacity overlay 450 ms, chyby neblikají. Text zprávy, stejné karty agentů, review tlačítka a emergency control se při nezměněném SSE záznamu znovu nevytvářejí.
- Skutečný aktuální stav úkolu má text i barevný marker; původní navázané záznamy zůstávají neutrální. Nejsou dopočítané fáze ani procenta. Přepnutí úkolu má fade 180 ms.
- Tablet/mobil: nativní modal drawer přesouvá stejné panely, nikoli kopie. Přesune se do něj i původní nouzový ovladač; vždy existuje právě jeden. Tab/Shift+Tab zůstává uvnitř, Escape zavře a vrátí fokus. Rozložení má hranice >1280 / 900–1280 / 600–899 / <600 px. Pod 600 px jsou ověřené dotykové cíle nejméně 44 × 44 px.
- Reduced motion vypíná dekorativní CSS i Web Animations; změna preference ruší právě běžící animace. Skrytá stránka/panel nespouští nové JS efekty.
- Quality Gate přirozeně rozšiřuje přehled, samostatnou stránku a detail úkolu; výchozí stav je připravuje se / bez výsledků. Výslovné demo pokrývá všechny stavy. Přepínání stránek/demo neodesílá požadavky na zápis. Analytics a Nastavení v požadované navigaci jsou jasně označené placeholdery, nikoli nové funkce backendu.

Podrobně: [Quality Gate kontrakt a práce čekající na B](QUALITY_GATE_UI_CONTRACT_2026-11-06.md).

## Screenshoty a datový původ

Před/po používá stejný dříve zachycený skutečný snímek místnosti (51 zpráv #651–701, pět účastníků). Jde o replay pro srovnání, nikoli současný produkční monitoring. QG je defaultně bez výsledků; soubor `quality-gate-demo.png` a mobilní QG snímek ukazují výslovně označené ukázkové záznamy `demo/*`. Testovací fixture je oddělené od těchto provozních záznamů.

| Zobrazení | Soubor |
| --- | --- |
| Před premium úpravou | [1339 × 758](ui-premium/before-reference-size.png) |
| Hlavní přehled s QG | [1920 × 1080](ui-premium/after-desktop-1920.png) |
| Notebook | [1366 × 768](ui-premium/after-laptop-1366.png) |
| Tablet | [768 px](ui-premium/after-tablet-768.png) |
| Drawer detailu | [Tablet](ui-premium/after-tablet-detail.png) |
| Mobil | [390 px](ui-premium/after-mobile-390.png) |
| Agenti v draweru | [Mobil](ui-premium/after-mobile-agents.png) |
| Quality Gate — skutečný prázdný stav | [Samostatná stránka](ui-premium/quality-gate-empty.png) |
| Quality Gate — jasně označené demo | [Detail a tabulka](ui-premium/quality-gate-demo.png) |
| Quality Gate — demo na mobilu | [390 px](ui-premium/quality-gate-mobile.png) |

## Ověření

- **26 browser scénářů PASS** v Chromiu, včetně původních akcí, payloadů, pause/resume, SSE/reconnect/dedup, XSS escapování, >200 zpráv, dlouhé/prázdné historie, motion časů, zachovaných DOM uzlů, online/offline/limited, QG prázdného stavu/všech ukázkových stavů/dlouhého názvu/detailu a nepřenášení demo PASS do skutečného úkolu.
- Desktop 1920 × 1080, notebook 1366 × 768, tablet 768 a mobil 390; doplňkové hranice 320, 599, 600, 899, 900, 1280, 1281 v pravidlech a 1440 px v základní sadě. Stránka nemá horizontální overflow; tabulky a kompaktní navigace mohou mít vlastní vodorovný scroll.
- Kontrolované paletové textové dvojice včetně krajních barev primárního gradientu/hover: minimálně **4,84 : 1**. Jde o měření definovaných barevných dvojic, nikoli úplnou certifikaci WCAG. [Výsledky kontrastu](ui-premium/contrast-results.json).
- Klávesnice, focus trap, Escape, návrat fokusu a reduced motion; 200% zvětšení testováno CSS zoomem v Chromiu. Není vydáváno za ruční certifikaci všech funkcí zoomu všech prohlížečů.
- Extrakční skript ověřuje bajtovou shodu Python kódu mimo `PAGE_HTML` vůči d01bf3e/d8e5795. Ruff, node syntax a diff-check.
- Plná Python regrese: **529 passed, 1 xfailed** v **138,58 s** (na izolované UI větvi). Ruff a node syntax PASS; bajtová shoda backendu mimo PAGE_HTML a diff-check PASS.

[Strojový browser report](ui-premium/browser-results.json) · [Diff od d8e5795](ui-premium/premium-frontend.patch)

### Opakování

```bash
python3 tests/ui/prepare_pages.py --base d8e5795
UI_PREMIUM=1 UI_OUTPUT=/tmp/agent-lease-premium-check \
PLAYWRIGHT_MODULE=/cesta/k/playwright-core CHROMIUM_PATH=/cesta/k/chrome \
  node tests/ui/browser_check.cjs
python3 -m pytest -q
ruff check src/agent_lease_mcp/webui.py tests/ui/prepare_pages.py
```

Lokální Chromium a HTTP/SSE fixture potřebují možnost vytvářet loopback sockety. Provozní databáze, tokeny ani služby se při testu nepoužívají. `node --check /tmp/agent-lease-ui-script.js` ověří vytažený skript. Frontendový patch je unified diff bez kontextu (`git apply --unidiff-zero`).

## Omezení a review

Skutečné QG napojení, scanner, prahy, pravidla verdiktů, coverage/CRAP adaptér, protokoly a úplná historie jsou práce pro fázi B po auditu architektury. Výkon 24 h a úspěšnost nejsou v dostupném API, proto nevznikl falešný graf. Existující reviews nejsou Quality Gate výsledky.

Větev neobsahuje mezitím vzniklé backendové commity Ady B1/B2/B5/B6. Před případným budoucím merge je třeba vyřešit konflikt ve webui.py a ověřit kompatibilitu s tehdejším backendem. Žádný backendový merge se nyní neprovádí. Noxova UI práce neblokuje Adiny backendové leases ani fázi A/A11.

Automatizace používá Chromium; Firefox/Safari ani fyzické dotykové zařízení nejsou ověřené. Snímky byly vlastní vizuální kontrolou prohlédnuty. Review má zkontrolovat datovou pravdivost, zachování původních bezpečnostních akcí, chování draweru a oddělení demo výsledků od úkolů. Po review a případných opravách bude nasazení/merge vyžadovat Michalovo schválení.

## Změněné soubory

- `src/agent_lease_mcp/webui.py` — jen obsah PAGE_HTML.
- `tests/ui/browser_check.cjs` — rozšířené scénáře a screenshoty.
- `tests/ui/premium_checks.cjs` — motion, DOM stabilita, drawer, touch, zoom a kontrast.
- `tests/ui/quality_gate_checks.cjs` — prázdný stav, demo, detail, navigace a oddělení od úkolů.
- `docs/QUALITY_GATE_UI_CONTRACT_2026-11-06.md` — prezentační struktura a budoucí integrace.
- Tento review dokument a `docs/ui-premium/*` — snímky před/po, JSON výsledky, kontrast a frontendový patch.

Úplný výpis: [changed-files.txt](ui-premium/changed-files.txt).
