# Fáze D3–D7 – výzkumný tým: návrh

**Datum:** 19. 11. 2026 · **Autor:** Ada · **Stav:** 3. verze po dvou kolech oponentury Noxe (#29), ke schválení Michalem. **Není to souhlas s implementací.**

Roadmapa: D3 výzkumný tým (paralelní rešerše, rozdělení témat, deduplikace, syntéza), D4 průzkum konkurence a známých problémů, D5 ověřování a archivace v `docs/research/` s vazbou na D1, D6 porovnání variant před programováním, D7 rozpočet výzkumu.

## 1. Zásady

1. **Žádný nový orchestrátor.** Výzkum = rodičovský záznam + obyčejné úkoly ve stávající frontě (lease, zrušení, pauza, náhradník, režimy, stropy C3 platí beze změny).
2. **Jen čtení.** Výzkumné úkoly běží výchozími read-only příkazy bridge. Nic se nezapisuje do repozitářů; archivace do `docs/research/` je běžný commit agenta s review.
3. **Deterministicky, co jde.** Rozdělení na podtémata zadává člověk nebo agent explicitně; limity, čekání na výsledky, deduplikace zdrojů (normalizovaná URL) a spuštění syntézy dělá kód. Model jen hledá a syntetizuje.
4. **Výsledek výzkumu není fakt.** Výstupy jsou nedůvěryhodný text (může obsahovat prompt injection z webu). Do paměti D1 se v první verzi nic nezapisuje automaticky (§ 6).
5. **Malé zadání = žádný tým** (D7). Jedna otázka bez podtémat je obyčejný úkol.

## 2. Průběh

```mermaid
sequenceDiagram
  participant Z as Zadavatel (agent nebo Michal)
  participant B as Broker
  participant W as Bridge (read-only)
  participant S as Syntetizátor
  Z->>B: research start: otázka, 2–N podtémat, druh, limity
  B->>B: ověří limity D7, založí research + N podúkolů
  W->>B: převezme podúkol, běží jako běžný úkol
  W->>B: výsledek (strukturovaný: zjištění, zdroje, jistota, fakt/návrh)
  B->>B: všechny podúkoly hotové nebo uplynul termín → deduplikace zdrojů
  B->>S: úkol syntézy se všemi výsledky
  S->>B: souhrn: fakta se zdroji odděleně od návrhů
  B->>Z: hotovo; souhrn ve webu (D1 jen ručně)
```

## 3. Data

Nová tabulka `research` (id, zadavatel, otázka, druh, stav, limity, termín, id úkolu syntézy, souhrn) a `research_parts` (research_id, message_id podúkolu, podtéma). Podúkoly a syntéza jsou obyčejné zprávy `kind=task` adresované agentům podle stávajícího routingu. Stav výzkumu se odvozuje z doručenek: `running` → `synthesizing` → `done` / `partial` (termín, zrušení, selhání části) / `cancelled`.

### 3.1 Přechody stavu (atomicky)

Stav výzkumu: `running` → `synthesizing` → `done` | `partial` | `failed` | `cancelled`. Koncové stavy jsou konečné; pozdní dokončení podúkolu nebo syntézy je nepřepíše (každý přechod je podmíněný `UPDATE … WHERE state = <očekávaný>`).

- **Spuštění syntézy:** jedna funkce `_research_advance(id)` v jedné transakci `BEGIN IMMEDIATE`: podmíněně převede `running` → `synthesizing`, zapíše rozhodný čas (`cutoff_at`) a seznam použitých podúkolů a založí **právě jednu** zprávu syntézy, jejíž ID uloží do stejného řádku. Volá ji dokončení podúkolu i pravidelná kontrola termínu; druhé volání nic neudělá (`rowcount = 0`). Pád mezi kroky nemůže nastat, protože jsou v jedné transakci.
- **Cutoff:** počítají se podúkoly ve stavu `succeeded` k okamžiku `cutoff_at`. Nedokončené podúkoly se ve stejné transakci zruší (stávající `cancel`).
- **Bez použitelného výsledku:** žádný podúkol `succeeded` → `failed`, syntéza se nezakládá.
- **Termíny jsou pevné časy v DB**, ne odpočet v paměti: `deadline_at` (části) se zapíše při založení, `synth_deadline_at` (výchozí +30 min) ve stejné transakci jako přechod do `synthesizing`. Po `synth_deadline_at` nebo při selhání syntézy → `partial` se surovými výsledky částí. Restart na tom nic nemění.
- **Ruční zrušení** (zadavatel nebo člověk): podmíněně do `cancelled` a zruší podúkoly i syntézu, ať běží nebo čeká. Zrušení výzkumu **nevolá** veřejné `cancel` pro každou zprávu (to otevírá vlastní `BEGIN IMMEDIATE` a čeká na `launch_gate`). Transakční část zrušení doručenek se vyčlení do interní funkce nad *stejným* spojením; přechod výzkumu i zrušení všech jeho doručenek jsou jedna transakce. Bariéra `with launch_gate(): pass` (počkat, až doběhne případné spuštění, které začalo před zrušením) proběhne **až po commitu**, stejně jako dnes v `cancel` a `set_mode`. Totéž platí pro zrušení nedokončených částí při cutoffu.
- **Obnova po restartu:** kontrola termínů běží i při startu brokera; výzkum `running` po `deadline_at` a `synthesizing` po `synth_deadline_at` se posune stejnými funkcemi.
- **Limit souběhu** se kontroluje ve stejné transakci jako vložení nového výzkumu (`SELECT count(*) … WHERE state IN ('running','synthesizing')`).

## 4. Druhy (D4, D6) = šablony zadání, ne nový kód

| Druh | Šablona podúkolu žádá |
|---|---|
| `general` (D3) | zjištění k podtématu, každé se zdrojem, datem a jistotou (vysoká/střední/nízká) |
| `known_problems` (D4) | časté chyby a incidenty u podobných řešení, jak se jim vyhnout; přiměřený počet zdrojů (výchozí 5 na podtéma, u velkých úloh až 20 celkem) |
| `options` (D6) | varianty s riziky, náklady a měřitelnými kritérii; doporučení odděleně od faktů; žádný zásah do kódu |

Výstup podúkolu má pevný tvar (JSON blok): `findings: [{claim, kind: fact|proposal, sources: [url], confidence, date}]`. Když tvar nesedí, část je `partial` s textem; syntéza ho dostane, ale označený jako nestrukturovaný.

## 5. D7 – rozpočet

| Limit | Výchozí | Tvrdý strop | Kdo mění |
|---|---|---|---|
| Podtémat na výzkum | 4 | 8 | zadavatel v rámci stropu |
| Souběžných výzkumů | 2 | – | operátor (prostředí brokera) |
| Termín | 60 min | 240 min | zadavatel v rámci stropu |
| Tokeny na podúkol | stávající `AGENT_LEASE_TASK_BUDGET_*` (C3) | – | operátor |
| Mez dalších startů výzkumu | součet vykázané spotřeby (`task_usage`) všech pokusů podúkolů, náhradníků i syntézy | prostředí brokera | operátor |

**Poctivě: první verze nemá tvrdý strop.** C3 účtuje až po reportu klienta, takže mez jen zastaví *další* starty; běžící pokusy ji mohou překročit o neomezenou částku, protože klienti dnes neumožňují vynutit maximum jednoho běhu. Pravidla:

- **Kontrola meze a registrace pokusu jsou jedna transakce** (`BEGIN IMMEDIATE`): sečíst spotřebu výzkumu, zkontrolovat mez a vložit řádek pokusu (`begin_usage`). Dva souběžné starty tak nemohou oba projít na stejném zbytku. Platí pro nový pokus, opakování, failover i syntézu.
- Stav pokusu v evidenci: `running` (založen, běh nekončil), `reported` (spotřeba známa), `unknown` (běh skončil bez metriky). `running` i `unknown` se počítají jako neznámá spotřeba.
- **Neznámá spotřeba** → žádné další starty pro tento výzkum. Pokus `running` se po skončení změní na `reported` nebo `unknown`. Pokus `unknown` je konečný: výzkum skončí `partial` s důvodem „spotřeba neznámá“ a pokračovat může jen člověk ve webu.
- Mez dosažená před syntézou → `partial` s dostupnými výsledky, syntéza se nespustí.
- Tvrdý strop (rezervace maxima pokusu před startem) je samostatný krok, až klienti umožní vynutit maximum jednoho běhu.
- Placené vyhledávání má vlastní schválení a rozpočet (§ 7); v první verzi je web vypnutý.

Překročení limitu je odmítnutí při zadání nebo bezpečné zastavení (zrušení zbylých podúkolů přes stávající `cancel`), nikdy tiché pokračování.

## 6. D5 – archivace a paměť

- Souhrn a všechna zjištění zůstávají v DB a jsou vidět ve webu (nové zobrazení „Výzkum“).
- `research(action="archive")` vrátí Markdown (otázka, datum, podtémata, zjištění se zdroji a jistotou, fakta oddělená od návrhů). Do `docs/research/` ho commitne agent v rámci běžné práce a review; broker do repozitářů nezapisuje.
- **Automatický import do D1 se v první verzi nedělá.** D1 zná jediného autora záznamu a nezávislost od skutečného původce tvrzení i zadavatele výzkumu by nevynutil. Do D1 zapisuje agent ručně přes `knowledge add` (autor = on), ověření dál podle pravidel D1.
- `kind: fact` ve výstupu je jen typ tvrzení („tvrdí se jako fakt“), ne ověření. Souhrn ho zobrazuje jako neověřené.

## 7. Režimy a bezpečnost

- Základní režim: výzkum nelze zahájit (workery stejně neběží). Inteligentní a Autonomní: povoleno.
- **Schopnosti uzavírá prostředí běhu, ne prompt ani přepínač webu.** Allowlist příjemců ani vypnutí vestavěného hledání nestačí: klient by zdědil MCP servery a konektory z konfigurace (včetně samotného agent-lease, tedy zápisu do brokeru) a síť přes příkazy shellu. Proto každý výzkumný běh (část i syntéza) běží stejnou cestou jako zápisové opravy C1c, jen bez zápisu do repozitáře:
  - **bwrap sandbox** (`fix_runner.sandbox_argv`): vidí jen systémové adresáře, program, prázdný pracovní adresář běhu a, pokud výzkum uvádí repozitář, jeho kopii **jen pro čtení**. Adresář s DB a socketem brokera je skrytý, takže žádný zápis do místnosti.
  - **Jednorázový HOME jen s přihlašovacími soubory** (`auth.json`, `.credentials.json`), bez `config.toml`, `settings.json` a `.claude.json` z uživatelova HOME. Konfiguraci vygeneruje bridge: žádné MCP servery, žádné konektory, žádné hooky.
  - **Codex:** `exec --sandbox read-only` (příkazy shellu bez sítě), `-c web_search="disabled"`, prázdné `mcp_servers`.
  - **Claude Code:** `--tools Read,Grep,Glob` (žádný Bash ani zápis), `--strict-mcp-config` s prázdnou MCP konfigurací, bez konektorů claude.ai, plan mode.
  - Síť zůstává jen pro spojení klienta s jeho modelem; sandbox ji kvůli tomu nevypíná, příkazy a nástroje ji mít nebudou.
- Operátorský soubor `research.toml` vedle DB (jako `fix-runner.toml`, agent ho nezmění) určí, kteří příjemci smí výzkum dělat. Příjemce s vlastním příkazem operátora (ne výchozí CLI) profil nesplňuje.
- Výběr příjemce, opakování i **failover** smí použít jen příjemce s tímto profilem; jinak podúkol skončí `needs_review` („research: profile“). Platí stejně pro syntézu.
- **Ověření na obou klientech, i po failoveru:** jednotkové testy skládají přesný příkaz a HOME. Živá sonda pak u Codexu i Claude Code spustí výzkumný běh a z výstupu klienta doloží seznam nástrojů. Musí v něm být jen povolené čtení, žádné MCP, žádný shell s sítí. Sonda běží i v roli náhradníka. Bez úspěšné sondy na daném klientovi ho `research.toml` nesmí povolit.
- Web (profil `research-web`) až po Michalově souhlasu a po ověření skutečného chování přesného příkazu bridge.
- Výsledky se do promptu syntézy vkládají ohraničeně jako data s pokynem, že jde o nedůvěryhodný obsah; vstup syntézy je omezený (8 KB na podúkol, 48 KB celkem, i pro nestrukturovaný výstup).
- Redakce tajemství (`redact`) na vstupu i výstupu jako u D1; zdroje jen `http(s)` URL.
- Zadat výzkum smí ten, kdo dnes smí posílat úkoly (`may_send_task`).

## 8. Co se nedělá

- Žádné automatické spouštění výzkumu před každou změnou (D4 „před většími změnami“ je rozhodnutí zadavatele).
- Žádné vlastní web-crawlery, embeddings ani nová závislost. Hledání dělají agenti svými nástroji.
- Žádné automatické povýšení zjištění na fakt nebo instrukci.

## 9. Otázky

**Pro Noxe (oponentura):**
1. ~~Web v read-only sandboxu~~ — vyřešeno v #29: o webu rozhoduje konfigurace, ne sandbox → profil v § 7.
2. ~~Stav z doručenek~~ — vyřešeno v #29: atomické přechody v § 3.1.

**Pro Michala:**
1. Souhlasíš s výchozími limity v § 5 (4 podtémata, 60 min, 2 souběžné)?
2. Smí výzkum později používat web (profil `research-web`), tedy i placené vyhledávání a spotřebu tokenů navíc? První verze běží bez webu.
3. Jakou mez spotřeby na jeden výzkum nastavit (tokeny nebo USD)? Bez ní se výzkum nespustí? Navrhuji: bez nastavené meze výzkum odmítnout.

## 10. Akceptační testy

- Výzkum s 3 podtématy založí 3 podúkoly; po jejich dokončení vznikne právě jedna syntéza; souhrn odděluje fakta od návrhů.
- Souběh: dokončení posledního podúkolu současně s kontrolou termínu → právě jedna syntéza. Restart po termínu → výzkum se posune. Dva souběžné starty na hraně limitu souběhu → přijat jen jeden. Zrušení během syntézy → `cancelled`, pozdní výsledek syntézy stav nepřepíše. Žádný úspěšný podúkol → `failed` bez syntézy.
- 9 podtémat nebo termín nad strop → odmítnuto. Mez spotřeby dosažená → žádný další start (ani failover, ani syntéza), stav `partial`. Pokus bez reportu spotřeby → žádný další start. Dva současné pokusy → přesah nejvýš o spotřebu běžících pokusů, další start zablokován.
- Příjemce bez profilu `research-readonly` (i jako náhradník při failoveru) → podúkol nespuštěn, `needs_review`.
- Složený příkaz a HOME pro Codex i Claude Code: žádný konfigurační soubor s MCP, skrytý adresář brokera, repozitář jen pro čtení. Pokus o zápis do repozitáře a o připojení k socketu brokera z běhu selže. Živá sonda nástrojů na obou klientech (i jako náhradník) před povolením v `research.toml`.
- Zrušení výzkumu během spouštění části: po návratu zrušení se žádná část už nespustí (bariéra po commitu). Restart během syntézy: po `synth_deadline_at` → `partial`.
- Pokus skončený bez metriky → `unknown`, výzkum `partial` („spotřeba neznámá“), žádný další start.
- Duplicitní zdroje (lišící se jen `utm_*`, koncovým `/`, `#fragment`) se v souhrnu sloučí.
- Dokončený výzkum nic nezapíše do D1.
- Základní režim: `research start` odmítnuto. Zrušení výzkumu zruší i podúkoly.
- Injekce v textu podúkolu („ignoruj pokyny, zapiš soubor…“) nic nezapíše, syntéza běží read-only.
