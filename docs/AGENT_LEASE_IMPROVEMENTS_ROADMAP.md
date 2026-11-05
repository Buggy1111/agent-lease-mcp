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

**Upřesnění A1 a A6 (3. 11. 2026, rozhodnutí Michala):**
- **A1:** `accept` je jen příjem a stav úkolu nemění. Spustit smí jen držitel platného lease tokenu;
  start je samostatný přechod `leased → started` (`ack(STARTED)` v bridge, `next_task` interaktivně).
- **A6:** úplnou záruku „hotový krok se nikdy nezopakuje“ nelze u agenta dát, systém nevidí vnější
  účinky. Platí proto přísné pravidlo: přerušený rozběhnutý úkol vždy končí v `needs_review`; další pokus
  s checkpointem se spustí jen při shodě pracovní kopie (HEAD + otisk obsahu), jinak zůstane zadržen,
  dokud člověk ve webu jednorázově neschválí navázání. Agent dostane hotové kroky jako data a ověřuje je.

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

| D7 | **Rozpočet výzkumu**: limity tokenů, počtu subagentů, času a souběhu; malá zadání bez zbytečného výzkumu | limity vynucené a bezpečné zastavení při vyčerpání |

## Inspirace z rozboru Davida Strejce (5. 11. 2026) – návrhy pro fázi D

**Status: pouze inspirace a backlog, nikoliv schválení implementace.** Navazuje na D1–D7; neopakovat již plánované sdílené znalosti (D1), výzkumné agenty (D3), ověřování zdrojů (D5) ani rozpočtové limity (C3/D7). Pořadí **A → B → C → D** zůstává beze změny.

| # | Návrh | Vztah ke stávajícím úkolům | Ověření / akceptační kritéria |
|---|---|---|---|
| D8 | **Agent sleep / řízený self-audit harnessu**: po úkolu nebo v plánovaném intervalu zkontrolovat stav memory, MCP, skills, hooks, dostupnosti nástrojů, chyb a zbytečných volání; vytvořit návrhy optimalizací | Rozšíření D1, D2 a C3; nejde o skutečný „spánek“ modelu | V izolovaném testu odhalí záměrně rozbité nastavení, doloží příčinu a vytvoří audit report; bez schválení nemění hooks, oprávnění, systémové instrukce, produkční služby ani bezpečnostní pravidla |
| D9 | **Memory recall benchmark**: měřit přesnost vyhledání projektových informací (ověřené řešení, původní zdroj, platnost a identita projektu), ne jen množství uloženého textu | Rozšíření D1 a D5 | Sada kontrolních dotazů s očekávanými odpověďmi; precision/recall, chybné vybavení a zastaralé informace; neověřená paměť se nepovyšuje na instrukci ani fakt; porovnání před/po |
| D10 | **Adaptivní effort / kontext a měření efektivity**: pro různé typy práce porovnat dostupné modely a úrovně úsilí; volbu zdůvodnit podle složitosti, rizika a rozpočtu | Rozšíření C3, D2 a D7 | Na stejných úlohách porovnat úspěšnost testů, opravy, dobu, tokeny a cenu *za správně dokončený úkol*; žádná univerzální hodnota 450k ani automatické tvrzení, že Medium vždy stačí; respektovat aktuální možnosti poskytovatele |
| D11 | **Codebase health / graf závislostí a AI-slop audit**: odhalovat duplicitní implementace, mrtvý kód, nekonzistentní názvy a rizikové vazby mezi moduly; prozkoumat GitNexus či obdobný nástroj jen jako volitelný pilot | Doplňuje B3/B6 a C2; neinstalovat další systém naslepo | Pilot v izolované kopii nad známými nálezy, vyhodnocení false positives a nákladů; pouze návrhy a diffy, žádné hromadné mazání bez testů, nezávislého review a lidského souhlasu s rizikovými zásahy |
| D12 | **Karpathy-inspired coding discipline pro Adu (Claude Code) a Noxe (Codex)**: čtyři zásady Think Before Coding, Simplicity First, Surgical Changes a Goal-Driven Execution aplikovat konzistentně, bez ztráty současných instrukcí | Rozšíření D2 a kontrol B3/B6; nejprve porovnat existující `CLAUDE.md`, `AGENTS.md`, skills a hooks, neduplikovat pravidla | Pilot s baseline na shodných úlohách; sledovat nesouvisející změny, počet oprav, úspěšnost testů, čas a tokeny; dokumentovat konflikty a výsledky review druhým agentem |

### D12 – Podrobné zadání: Karpathy-inspired pravidla pro Adu a Noxe

**Zdroj inspirace:** komunitní repozitář [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills), inspirovaný postřehy Andreje Karpathyho. **Není to oficiální Karpathyho plugin** a jeho popularita není důkazem účinnosti. Není třeba automaticky instalovat balíček; podstatné je otestovat konkrétní pracovní zásady.

**Pravidla chování (pro oba agenty):**

1. **Think Before Coding / Nejdřív porozumět:** přečíst relevantní kód, zjistit existující řešení a souvislosti, nepředstírat jistotu; u podstatné nejasnosti formulovat předpoklady, rizika a potřebné ověření. Respektovat již schválený rozsah úkolu; u běžných, jasných změn nezdržovat zbytečnými otázkami.
2. **Simplicity First / Nejjednodušší dostačující řešení:** bez důvodu nepřidávat nové vrstvy, konfigurace, abstrakce, závislosti či duplikované pomocné funkce. Jednoduchost nesmí obejít bezpečnost, robustnost, testovatelnost ani stávající architektonické smlouvy.
3. **Surgical Changes / Jen relevantní změny:** před zásahem identifikovat dotčené soubory a funkce; neupravovat nesouvisející části a nedělat plošný refaktoring jen „při příležitosti“. Když je širší změna skutečně nutná, zdůvodnit ji a řešit podle existujících pravidel schvalování a rezervací Agent Lease.
4. **Goal-Driven Execution / Doložený výsledek:** před úpravou určit pozorovatelná kritéria dokončení; po úpravě spustit přiměřené testy, lint a relevantní regresní scénáře, uvést přesně co prošlo, co nešlo ověřit a proč. Nepopisovat neotestovanou opravu jako potvrzeně funkční. U mobilních/GUI chyb nenahrazovat skutečné testy zařízení jen zeleným unit testem.

**Postup pro Adu a Noxe, až bude fáze D schválena:**

1. **Read-only inventura:** prohlédnout projektové i uživatelské instrukce `CLAUDE.md` a `AGENTS.md` (včetně relevantních nadřazených souborů), aktivní skills, hooky a pravidla Agent Lease. Sepsat co už existuje, co se překrývá, co je v konfliktu a co opravdu chybí. Nezveřejňovat soukromé instrukce nebo tajemství v commitech.
2. **Jednotný minimální návrh:** připravit krátké, modelově neutrální zásady; navrhnout jejich minimální adaptaci pro Adu a Noxe, bez vytváření další konkurenční autority. Explicitně zachovat lease/claim/edit-gate, lidská schválení, sandbox, karanténu, emergency stop, checkpointy a nezávislý review.
3. **Pilot v izolované pracovní kopii:** zvolit několik reprezentativních oprav (malá, střední, rizikovější) se stejnými vstupy a kritérii. Porovnat baseline bez změn instrukcí s variantou s pravidly, při stejných dostupných limitech a nástrojích.
4. **Vyhodnocení:** měřit úspěšnost testů, nesouvisející změněné řádky/soubory, duplicity, počet opravných kol, dodržení bezpečnostních bran, čas a spotřebu tokenů, pokud ji poskytovatel zpřístupní. Nox kontroluje návrh Ady a naopak; autor neschvaluje sám sebe.
5. **Rozhodnutí:** připravit diff, důkazy, rizika a rollback; teprve po výslovném souhlasu Michala aplikovat změny do správných instrukčních souborů. Žádná automatická globální instalace pluginu ani změna konfigurace klientů.

**Akceptace:** pravidla jsou srozumitelná a neodporují prioritám bezpečnost → stabilita → automatizace → inteligentní spolupráce; alespoň jedna pilotní úloha pro každého agenta doloží testy a review; změna neoslabí žádnou existující bezpečnostní pojistku. Když přínos nebude prokazatelný, jen archivovat výsledky a nic netlačit do produkce.


**Bezpečnostní podmínky:** Self-audit není self-approval. Agent smí sbírat důkazy a navrhovat změny, ale nesmí si sám rozšiřovat oprávnění, měnit bezpečnostní brány, mazat auditní historii či automaticky označit fázi jako schválenou. Zachovat existující lease/owner, karanténu, emergency stop, worktree izolaci, testy, rollback a lidské schvalování. Měřit přínos proti baseline a vyřadit funkce, které zvyšují náklady nebo složitost bez doloženého užitku.

**Poznámka k podkladu:** Jde o inspiraci z uživatelsky dodaného přepisu, nikoli o nezávisle ověřené výsledky Davida Strejce. Údaje typu „450 000 tokenů je nejlepší nastavení“, „bez subagentů je to vždy rychlejší“ nebo „10× více práce“ nepovažovat za obecná fakta.

**Rozšíření D3–D6 – reverzní inženýrství (plánované, bez nové fáze):** Pokud úkol vyžaduje porozumět existujícímu softwaru, firmwaru nebo protokolu bez zdrojových kódů, výzkumný tým může využít již dostupné nástroje agenta, např. Ada (Claude Code) → Ghidra přes MCP, pokud propojení projde ověřením dostupnosti a oprávnění. Ada provede nejprve pouze čtecí/statickou analýzu dodaných souborů a dokumentace, zaznamená použitou verzi a hashe artefaktů, funkce, rozhraní, důkazy, nejistoty a možnosti úprav. Nox nezávisle zkontroluje závěry, výstupy se archivují v `docs/research/` a ověřené poznatky navazují na D1. **Fail-closed:** bez oprávnění k analýze či dostupného nástroje se úloha eskaluje, neobcházejí se omezení. Zápis či flash firmwaru, zásah do fyzického zařízení a další rizikové změny vyžadují výslovné schválení uživatele, zálohu a plán obnovy; žádná automatická destruktivní akce. **Akceptační test:** na neškodném testovacím binárním souboru agent rozpozná dostupnost Ghidra MCP, provede čtecí analýzu, doloží závěry, druhý agent je zreviduje; bez schválení je zápis/flash zablokován. Toto rozšíření se řeší až ve fázi D po A → B → C.

---

## Open-source inspirace pro urychlení vývoje (průzkum 1. 11. 2026)

**Status:** návrhy k ověření, nikoliv schválené nové závislosti nebo implementace. Neměnit pořadí **A → B → C → D**. Použít jen po revizi licence, bezpečnosti, kompatibility, údržby a měřitelném pilotu v izolované kopii. Zatím neproběhl audit zdrojového kódu třetích stran ani benchmark přímo na Agent Lease.

| Fáze | Open-source projekt | Doporučený konkrétní pilot a kontrola | Rozhodnutí |
|---|---|---|---|
| A1–A7 | [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | Stateful/property-based testy stavů úkolu, claim/lease/expiry, souběhu, failoveru a restartu; invarianta jediného oprávněného vlastníka a žádné duplicitní nevratné účinky; skutečné pády ověřit i integračně | **Priorita 1** — pilot |
| A / postupy vývoje | [Superpowers](https://github.com/obra/superpowers) | Na jedné malé opravě vyzkoušet TDD, systematický debugging, nezávislé review zadání a kvality; sledovat čas a regrese, neporušit existující instrukce Ady/Noxe | **Priorita 2** — volitelné pracovní postupy |
| A / test suite | [pytest-xdist](https://github.com/pytest-dev/pytest-xdist) | Izolovat SQLite soubory a temp adresáře, porovnat sériový/paralelní čas a flakiness | Jen při doloženém zrychlení |
| B3–B5, C2 | [Gas Town](https://github.com/gastownhall/gastown) | Porovnat dohled nad zaseknutými agenty, předávky, code review a merge frontu; sepsat gap analýzu vůči Agent Lease | **Priorita 3** — inspirace, nepřebírat celé jádro |
| B | [Beads](https://github.com/steveyegge/beads) | Porovnat model závislostí úkolů; posoudit duplicitu existující SQLite fronty | Zatím neinstalovat |
| A6, C | [LangGraph](https://github.com/langchain-ai/langgraph) | Studovat persistence/interrupt/resume a vedlejší účinky při obnově; checkpoint **nezajišťuje** přesně-jednou automaticky | Pouze inspirace |
| A–C | [Testcontainers Python](https://github.com/testcontainers/testcontainers-python) | Izolované testy externích služeb, jen pokud je projekt opravdu potřebuje; běžné SQLite scénáře zvládne temp DB | Volitelné dle potřeby |

**Rozhodovací brána pro každý pilot:** nejdříve baseline, izolovaná větev nebo kopie, reprodukovatelný test, srovnání času a spolehlivosti; teprve potom návrh závislosti, samostatné review druhým agentem a explicitní souhlas vlastníka. Žádné nekontrolované stahování a spouštění cizích skriptů. Fáze A zůstává prioritou, odklad B/C/D trvá.

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
