# Návrh: neonové UI podle Michalových předloh — ke schválení

**Zdroj:** `Agent_Lease_KOMPLET_UI_Design_Assets_README.zip` (6 obrazovek, README, portréty Ady a Noxe).
**Michal 21. 11.:** Místnost agentů je klíčová a zůstává; dominantní panel na Přehledu + samostatná položka
„Místnost agentů“ přes celou plochu; nic nemazat jen kvůli vzhledu; **funkčnost místnosti před efekty**;
před implementací analýza, návrh a schválení. Stav: **schváleno Michalem 21. 11.** (Katalog = koncept bez dat, navigace vodorovně s položkou Místnost agentů, jen tmavý režim podle předlohy); implementace na větvi `ada/ui-neon-20261121`.

## 1. Co dnes existuje (audit `webui.py`, jen čtení)

- Jedna stránka `PAGE_HTML` (CSS + HTML + vanilla JS), přihlášení tokenem → cookie, data ze
  `/api/snapshot` a živě přes SSE `/api/events`. API: `send`, `pause/resume`, `cancel/retry`,
  `approve*`, `mode`, `knowledge*`, `research*`, `review-*`, `quality-*`.
- **Místnost:** `renderRoom`, filtry, hledání, odesílání zprávy/úkolu (`/api/send`), adresát z `<select id="to">`,
  akce u úkolů (zrušit, zopakovat, schválit karanténu/opravu), detail úkolu a časová osa, nouzové zastavení.
- **Stav agentů (skutečná data):** `peers` (presence, `seen_seconds_ago`, status text, `provider_state`)
  a `board` (`active` úkol, `queued`, `needs_review`, `blocked_reason`) pro `claude-code`, `codex`
  a jejich mosty `*-bridge`.
- Stránky: Kvalita (B7 běhy, fronta), Výzkum (D3), Paměť (D1), Nastavení (režim). Katalog **neexistuje**
  (je jen dokument `docs/research/2026-11-19-d-capabilities-*.md`, žádné API ani datový model).
- Testy: `tests/test_webui.py` (25) hlídají přihlášení, oprávnění, SSE, pauzu, `<select id="to">` aj.

## 2. Rozdílová analýza předloh

| Prvek z předlohy | Stav | Návrh |
|---|---|---|
| Hlavička: ∞ logo, název, podtitul, karty Ada/Nox s portrétem, stav, křivka aktivity | portréty a karty **chybí**, data o stavu **existují** | postavit; portréty zmenšit (~240 px WebP, ~40 kB) a servírovat z balíčku (`/assets/ada.webp`, `/assets/nox.webp`) |
| Pulzování jen při skutečné práci, nezávisle | data **existují** | mapování v § 3; animace jen `opacity/transform`, `prefers-reduced-motion` vypne |
| Odznak „Fáze D: katalog schopností“, zvonek, nápověda, avatar „B“ | ilustrace | odznak = **aktuální režim** (Inteligentní/Autonomní); zvonek = počet věcí čekajících na člověka (needs_review + nepřevzaté); avatar „M“ |
| Navigace Přehled · Kvalita · Výzkum · Paměť · Katalog · Nastavení | 5 z 6 existuje | + **Místnost agentů** (přes celou plochu); Katalog viz § 4 |
| Řádek 6 metrik na každé stránce | částečně | jen skutečná čísla (§ 4) |
| **Místnost agentů** | **existuje, chybí v předloze** | dominantní panel na Přehledu (místo grafu postupu) + samostatná stránka; **stejné JS funkce a API, žádná změna backendu** |
| Graf „Celkový postup v čase“ | **data neexistují** | nevytvářet (README: nevyrábět trend) |
| Aktivita agentů (časová osa vpravo) | **existuje** (zprávy/události) | časová osa ze skutečných zpráv Ady/Noxe, řazení podle času |
| Projekty s % průběhu | částečně (repozitáře v review/kvalitě, bez %) | seznam repozitářů se stavem posledního review/kontroly, bez vymyšlených % |
| Kvalita: skóre 92 %, chybovost, readiness | **definice neexistuje** | místo skóre počty PASS / FAIL / REVIEW / N/A z běhů B7, review stavy |
| Výzkum: úkoly, etapy, zjištění, zdroje | **existuje** (research, části, syntéza, URL zdrojů, model, spotřeba) | etapy = Zadání → Části → Syntéza → Hotovo (skutečné stavy D3) |
| Paměť: hledání, souhrny, rozhodnutí | **existuje** (knowledge, FTS) | dominantní hledání, seznam ověřených/neověřených |
| Nastavení: agenti, modely, režim | částečně (režim, modely z `research.toml`) | režim (funkční), modely jen ke čtení; **bez** integrací, oprávnění, „Bezpečnost 100 %“, 2FA |
| Integrace GitHub/Slack/Notion, „Připojeno“ | ilustrace | nezobrazovat |

## 3. Stav Ady a Noxe → pulzování (jen skutečná data)

Ada = `claude-code` + `claude-code-bridge`, Nox = `codex` + `codex-bridge`. Každý se počítá **zvlášť**.

| UI stav | Podmínka ze snapshotu/SSE | Vzhled |
|---|---|---|
| **Pracuje** | most má `board.active` (běžící úkol) **nebo** interaktivní relace `presence=busy`, `seen < 120 s` a status není „start session“ | pulz své barvy 2,2 s, text „Pracuje“ + název úkolu/status |
| **Čeká** | relace nebo most online, nic neběží | barva bez pulzu, „Čeká“ |
| **Limit** | `provider_state=limited` | tlumená barva + „Limit do HH:MM“ |
| **Chyba** | `blocked_reason` nebo `needs_review>0` | ikona ⚠ + text, **bez pulzu** |
| **Offline** | relace i most `seen` > práh | utlumené, „Offline“ |
| **Neznámý stav** | SSE spojení přerušeno / data starší než 60 s | šedé, „Spojení přerušeno“ — nikdy nezůstane „Pracuje“ |

## 4. Obsah stránek (jen skutečná data)

- **Přehled:** metriky aktivní agenti · běží · ve frontě · ke kontrole · otevřená review · kontroly kvality (PASS/FAIL);
  vlevo repozitáře, **uprostřed Místnost agentů** (celá funkčnost), vpravo Aktivita agentů; pod tím aktivní úkoly a review.
- **Místnost agentů:** totéž přes celou plochu + detail úkolu v panelu.
- **Kvalita / Výzkum / Paměť / Nastavení:** stávající funkce v novém vzhledu podle předloh.
- **Katalog:** zatím bez dat → navrhuji stránku **jen ke čtení** se souhrnem z dokumentu fáze D a jasným štítkem
  „koncept, data zatím nejsou v aplikaci“ (alternativa: položku zatím skrýt).

## 5. Vzhled

Tokeny podle README: pozadí `#070D20`, panel `#101B38`, fialová `#923BFF` (aktivní/primární), azurová `#04CFFF`,
Ada `#FF8A18`, Nox `#00E779`, text `#F5F7FF`, vedlejší `#BBC6E1`. Tenké svítící okraje, jemný glow jen u aktivního
agenta, kontrast textu ≥ 4,5 : 1 (ověřím měřením), mobil: panely pod sebou. Jen tmavý režim (jako předloha).

## 6. Co se změní (soubory)

- `src/agent_lease_mcp/webui.py`: nová vrstva CSS a HTML kostra, **stávající ID a JS funkce místnosti zůstanou**
  (`renderRoom`, `send`, `fillRecipients`, `#to`, `#feed`, `#compose`, pauza, akce úkolů); nové funkce
  `agentStatus()`, `renderAgentCards()`, `renderActivity()`; routy `/assets/*.webp` a `/favicon.svg`.
- `src/agent_lease_mcp/assets/ada.webp`, `nox.webp` (zmenšené portréty) + zahrnutí do balíčku (`pyproject.toml`).
- Opravy: 404 favicon, 403 při startu (prověřit pořadí přihlášení vs. první požadavek), kontrast z review #48.
- Review #48 (Apple styl) zavřít jako nahrazené.

## 7. Testy a ověření

- `tests/test_webui.py` zůstává zelený; nové testy pro routy assetů/favicon a pro mapování stavu (čistá JS funkce
  ověřená v prohlížeči skriptem: Ada pracuje / Nox pracuje / oba / nečinní / offline / chyba / neznámý / limit).
- Snímky všech sekcí (1440, 1024, 390) a porovnání s předlohami; měření kontrastu; `prefers-reduced-motion`.
- Nezávislé review Noxem; nasazení až po tvém souhlasu.

## 8. Otevřené otázky pro Michala

1. Katalog: read-only koncept z dokumentu, nebo zatím skrýt?
2. „Do levé navigace“ — předloha má navigaci vodorovně pod hlavičkou; dávám tam položku **Místnost agentů**. Souhlas?
3. Jen tmavý režim (jako předloha), nebo zachovat i světlý?
