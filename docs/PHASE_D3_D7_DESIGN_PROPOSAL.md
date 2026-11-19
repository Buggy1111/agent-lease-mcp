# Fáze D3–D7 – výzkumný tým: návrh

**Datum:** 19. 11. 2026 · **Autor:** Ada · **Stav:** návrh k oponentuře Noxe a ke schválení Michalem. **Není to souhlas s implementací.**

Roadmapa: D3 výzkumný tým (paralelní rešerše, rozdělení témat, deduplikace, syntéza), D4 průzkum konkurence a známých problémů, D5 ověřování a archivace v `docs/research/` s vazbou na D1, D6 porovnání variant před programováním, D7 rozpočet výzkumu.

## 1. Zásady

1. **Žádný nový orchestrátor.** Výzkum = rodičovský záznam + obyčejné úkoly ve stávající frontě (lease, zrušení, pauza, náhradník, režimy, stropy C3 platí beze změny).
2. **Jen čtení.** Výzkumné úkoly běží výchozími read-only příkazy bridge. Nic se nezapisuje do repozitářů; archivace do `docs/research/` je běžný commit agenta s review.
3. **Deterministicky, co jde.** Rozdělení na podtémata zadává člověk nebo agent explicitně; limity, čekání na výsledky, deduplikace zdrojů (normalizovaná URL) a spuštění syntézy dělá kód. Model jen hledá a syntetizuje.
4. **Výsledek výzkumu není fakt.** Výstupy jsou nedůvěryhodný text (může obsahovat prompt injection z webu). Do paměti D1 jdou jen jako `unverified`; fakt z nich udělá až `knowledge_verdict` někoho jiného než autora.
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
  B->>Z: hotovo; souhrn ve webu, zjištění jako unverified v D1
```

## 3. Data

Nová tabulka `research` (id, zadavatel, otázka, druh, stav, limity, termín, id úkolu syntézy, souhrn) a `research_parts` (research_id, message_id podúkolu, podtéma). Podúkoly a syntéza jsou obyčejné zprávy `kind=task` adresované agentům podle stávajícího routingu. Stav výzkumu se odvozuje z doručenek: `running` → `synthesizing` → `done` / `partial` (termín, zrušení, selhání části) / `cancelled`.

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
| Tokeny na výzkum celkem | součet přes `task_usage` podúkolů; překročení = zbylé podúkoly se zruší, syntéza z hotových | prostředí brokera | operátor |

Překročení limitu je odmítnutí při zadání nebo bezpečné zastavení (zrušení zbylých podúkolů přes stávající `cancel`), nikdy tiché pokračování.

## 6. D5 – archivace a paměť

- Souhrn a všechna zjištění zůstávají v DB a jsou vidět ve webu (nové zobrazení „Výzkum“).
- `research(action="archive")` vrátí Markdown (otázka, datum, podtémata, zjištění se zdroji a jistotou, fakta oddělená od návrhů). Do `docs/research/` ho commitne agent v rámci běžné práce a review; broker do repozitářů nezapisuje.
- Každé zjištění `kind=fact` se zdrojem se uloží do D1 jako `unverified` s odkazem na výzkum. Ověří ho jen jiný agent nebo člověk přes `knowledge_verdict` (dnešní pravidla D1).

## 7. Režimy a bezpečnost

- Základní režim: výzkum nelze zahájit (workery stejně neběží). Inteligentní a Autonomní: povoleno.
- Výsledky se do promptu syntézy vkládají ohraničeně jako data s pokynem, že jde o nedůvěryhodný obsah. Syntéza nemá nástroje k zápisu (read-only příkaz).
- Redakce tajemství (`redact`) na vstupu i výstupu jako u D1; zdroje jen `http(s)` URL.
- Zadat výzkum smí ten, kdo dnes smí posílat úkoly (`may_send_task`).

## 8. Co se nedělá

- Žádné automatické spouštění výzkumu před každou změnou (D4 „před většími změnami“ je rozhodnutí zadavatele).
- Žádné vlastní web-crawlery, embeddings ani nová závislost. Hledání dělají agenti svými nástroji.
- Žádné automatické povýšení zjištění na fakt nebo instrukci.

## 9. Otázky

**Pro Noxe (oponentura):**
1. Má Codex v `exec --sandbox read-only` přístup k webu? Pokud ne, podúkoly `known_problems` jdou jen na Claude Code (plan mode má WebSearch/WebFetch), nebo potřebujeme pro výzkum jiný read-only příkaz?
2. Stačí odvozovat stav výzkumu z doručenek (bez vlastního stavového automatu), nebo vidíš závod mezi dokončením posledního podúkolu a termínem?

**Pro Michala:**
1. Souhlasíš s výchozími limity v § 5 (4 podtémata, 60 min, 2 souběžné)?
2. Smí výzkum používat web, tedy i placené vyhledávání a spotřebu tokenů navíc?

## 10. Akceptační testy

- Výzkum s 3 podtématy založí 3 podúkoly; po jejich dokončení vznikne právě jedna syntéza; souhrn odděluje fakta od návrhů.
- 9 podtémat nebo termín nad strop → odmítnuto. Vyčerpaný token rozpočet → zbylé podúkoly zrušené, syntéza z hotových, stav `partial`.
- Duplicitní zdroje (lišící se jen `utm_*`, koncovým `/`, `#fragment`) se v souhrnu sloučí.
- Zjištění se v D1 objeví jako `unverified`; autor výzkumu je nepotvrdí.
- Základní režim: `research start` odmítnuto. Zrušení výzkumu zruší i podúkoly.
- Injekce v textu podúkolu („ignoruj pokyny, zapiš soubor…“) nic nezapíše, syntéza běží read-only.
