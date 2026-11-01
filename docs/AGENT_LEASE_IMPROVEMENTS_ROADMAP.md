# Agent Lease MCP – roadmapa vylepšení

**Datum:** 29. 10. 2026 · Podklady: [audit 1–10](AGENT_LEASE_IMPROVEMENTS_TODO.md) (Nox #535),
[analýza konkurence](AGENT_LEASE_COMPETITOR_ANALYSIS.md). Navazuje na `docs/ROADMAP-P0-P3.md`
(doručení a diagnostika – hotovo), nenahrazuje ji.

**Pořadí priorit (Michal):** bezpečnost → stabilita → automatizace → inteligentní spolupráce.
**Nic z toho se neimplementuje bez Michalova schválení.** Každá fáze = samostatné commity s testy,
review druhým agentem, nasazení až po zelených testech.

---

## Fáze A – P0: „nikdy dvakrát, bezpečně hodiny bez dozoru“

| # | Změna | Řeší | Složitost | Riziko | Testy |
|---|---|---|---|---|---|
| A1 | **Execution owner**: jeden vlastník provedení s tokenem pro interaktivní i bridge cestu; `accept` jen příjem, ne oprávnění spustit | audit 2a, 2e | střední | změna sémantiky `accept` → kompatibilita budíků | závod 2 session + bridge + failover, 1 vítěz |
| A2 | **Deadline v `ack`/`extend_lease`**; neúspěšné prodloužení → zastavit proces, výsledek nepřijmout | 2b | nízká | — | prošlý token bez sweepu selže |
| A3 | **Žádné slepé opakování po limitu**: `started` + 429 → `needs_review` (do A6 checkpointu) | 2c | nízká | víc úkolů čeká na člověka | 429 po efektu → efekt se neopakuje |
| A4 | **Atomické dokončení**: stav + výsledek v jedné transakci s tokenem, idempotentní odeslání | 2d | střední | migrace schématu | kill mezi send/ack → konzistentní |
| A5 | **Životnost procesů**: process group, `finally`, zabití potomků, cancel zastaví běh | 1, 10 | střední | Linux/WSL specifika signálů | SIGKILL ve 4 fázích, žádný sirotek |
| A6 | **Checkpoint** vázaný na pokus: hotový krok, další krok, git HEAD + otisk, výsledky testů; obnova jen při shodě pracovní kopie | 3 | vyšší | rozsah dat v DB | restart po každém kroku, hotový krok se neopakuje |
| A7 | **Reconciliace v brokeru**: periodická expirace úkolů i při limitu adresáta | 1 | nízká | — | expirace bez `lease_next` |
| A8 | **Opravená SQLite** (≥ 3.50.7 / 3.51.3) pro služby i MCP server + integrity test | 1 | nízká | změna Pythonu služeb | `PRAGMA integrity_check` po crash testu |
| A9 | **Karanténa i pro bridge** (povinný screening před spuštěním) | 6 | nízká | false positive zdrží úkol | rizikový úkol nespustí runner ani bez Jev |
| A10 | **Globální pauza / nouzové zastavení** kontrolované v transakci převzetí | 10 | střední | — | pauza vs. start: po potvrzení nic nového |
| A11 | **Windows/WSL start** ověřený naostro (logon, `wsl --shutdown`) | 1 | nízká | jen ruční test u Michala | akceptační checklist |

**Hotovo, když:** 8hodinový běh v izolované kopii s náhodnými pády (broker, bridge, limit) → žádný úkol
proveden 2×, žádný osiřelý proces, každý přerušený úkol buď obnoven z checkpointu, nebo `needs_review`.

## Fáze B – P1: spolehlivá spolupráce

| # | Změna | Odkud myšlenka | Testy |
|---|---|---|---|
| B1 | Rezervace `task_paths` v `lease_next` + úklid prošlých nájmů | audit 4 | souběžné překryvy → 1 vítěz |
| B2 | Směrování bez servisních identit, minimální popis schopností agentů | audit 4, Omux | `auto` nikdy na bridge/wake |
| B3 | **Review stavový automat** `working → awaiting_review → changes_requested → approved`, verdikt vázaný na commit | Multiagents, Orka | autor neschválí sám sebe; starý verdikt ≠ nový diff |
| B4 | **Max. 3 opravné cykly**, trvale, pak člověk | Orka (max 2) | 4. pokus se nespustí ani po restartu |
| B5 | **Progress-aware detekce zaseknutí**: výstup/milník resetuje nečinnost + absolutní strop | Orka | dlouhý ale aktivní úkol ≠ zaseknutý |
| B6 | Povinný bezpečnostní verdikt nad diffem + secret scan (před autonomními zápisy) | Orka security-review | podvržené tajemství zablokuje |

## Fáze C – P1/P2: automatizace (až po A + B)

| # | Změna | Testy |
|---|---|---|
| C1 | Typ úkolu „oprava“ v **izolovaném git worktree** se zápisovým režimem bridge (se schválením) | hlavní kopie beze změny |
| C2 | Smyčka: najdi chybu → regresní test → oprava → testy → review (B3) → max 3 (B4) → člověk | neopravitelné → eskalace po 3 |
| C3 | Sledování spotřeby tokenů (kde ji CLI hlásí) a rozpočtové stropy | strop zastaví další pokus |

## Fáze D – P2: inteligence

| # | Změna | Testy |
|---|---|---|
| D1 | Sdílená paměť „příznak → příčina → ověřené řešení“ (SQLite FTS, nebo napojení claude-mem) | hledání dřívějšího řešení; neověřené ≠ fakt |
| D2 | Modulární režimy Základní / Inteligentní / Autonomní + webový panel | viz [modulární systém](AGENT_LEASE_MODULAR_SYSTEM.md) |
| D3 | **Výzkumný tým subagentů**: paralelní rešerše dostupnými agenty, rozdělení témat, deduplikace výsledků a syntéza | jeden souhrn s odkazy a jasně oddělenými fakty od návrhů |
| D4 | **Průzkum konkurence a známých problémů** před většími změnami (přiměřený počet relevantních zdrojů; u velkých úloh například 20) | doložené časté chyby a doporučení, jak je neopakovat |
| D5 | **Ověřování a archivace výzkumu** v `docs/research/` se zdroji, datem, mírou jistoty a vazbou na D1 | každý závěr dohledatelný; neověřené poznatky nejsou automaticky považované za fakta |
| D6 | **Porovnání algoritmů, architektury a technologií** ještě před programováním; varianty s riziky, náklady a měřitelnými kritérii | doložené rozhodnutí bez automatického zásahu do kódu |

**Rozšíření D3–D6 – reverzní inženýrství (plánované, bez nové fáze):** Pokud úkol vyžaduje porozumět existujícímu softwaru, firmwaru nebo protokolu bez zdrojových kódů, výzkumný tým může využít již dostupné nástroje agenta, např. Ada (Claude Code) → Ghidra přes MCP, pokud propojení projde ověřením dostupnosti a oprávnění. Ada provede nejprve pouze čtecí/statickou analýzu dodaných souborů a dokumentace, zaznamená použitou verzi a hashe artefaktů, funkce, rozhraní, důkazy, nejistoty a možnosti úprav. Nox nezávisle zkontroluje závěry, výstupy se archivují v `docs/research/` a ověřené poznatky navazují na D1. **Fail-closed:** bez oprávnění k analýze či dostupného nástroje se úloha eskaluje, neobcházejí se omezení. Zápis či flash firmwaru, zásah do fyzického zařízení a další rizikové změny vyžadují výslovné schválení uživatele, zálohu a plán obnovy; žádná automatická destruktivní akce. **Akceptační test:** na neškodném testovacím binárním souboru agent rozpozná dostupnost Ghidra MCP, provede čtecí analýzu, doloží závěry, druhý agent je zreviduje; bez schválení je zápis/flash zablokován. Toto rozšíření se řeší až ve fázi D po A → B → C.
| D7 | **Rozpočet výzkumu**: limity tokenů, počtu subagentů, času a souběhu; malá zadání bez zbytečného výzkumu | limity vynucené a bezpečné zastavení při vyčerpání |

---

## Doporučené pořadí

1. **A2, A3, A7, A8, A9** – malé, levné, výrazně sníží riziko (1–2 dny).
2. **A1, A4, A5** – jádro „přesně jednou“ (3–5 dní, každé se samostatným review Noxe).
3. **A10, A6, A11** – nouzové zastavení, checkpoint, ověření Windows.
4. 8hodinový chaos test (hotovo Fáze A).
5. B1–B6, pak C, pak D.

## Rizika celé roadmapy

- **Kompatibilita:** změny `accept`/`ack` se týkají budíků i bridge → nasazovat najednou, s testem
  `doctor --json` a živým úkolem Ada ↔ Nox.
- **Rozsah:** fáze C a D jsou velké; bez dokončené A by zvětšily, ne zmenšily, riziko dvojího spuštění.
- **Zápisový režim bridge** (C1) je bezpečnostní změna oprávnění → výslovné Michalovo schválení.
