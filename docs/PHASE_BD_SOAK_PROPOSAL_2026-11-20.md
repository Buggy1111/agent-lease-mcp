# Pokrytí zátěže B–D a návrh malého soak

20. 11. 2026, Nox, úkol #1670 od Ady. Prohlédnutý základ: main `5e8a171caab67fdcf30845fd7cdd093a4f6bf2aa`, shodný s ohlášeným základem Adina 8h chaosu. Toto je read-only průzkum a návrh. Nový harness není implementovaný a žádný zátěžový běh nebyl spuštěn.

## Co osmihodinový chaos ověřuje

`tests/chaos/run_chaos.py:104` spouští skutečný izolovaný broker a více bridge stejného syntetického agenta. Driver (`:159–217`) vytváří běžné tasky, zkouší karanténu, pády, cancel, interaktivní převzetí a pauzu. Falešný worker simuluje limit a nevratný efekt, občas přes potomka. Vyhodnocení (`:249–345`) kontroluje dvojité/falešné Done a efekty, karanténu, pauzu, sledované procesy, dojezd a integritu. Vyžaduje skutečně pozorované pokrytí uvedených scénářů.

**Nevytváří žádné review, fix task, research ani quality request.** Novější moduly mohou být importované a broker může pouštět jejich prázdné sweepy/worker, ale to není pokrytí jejich stavových přechodů. Prošlý 8h běh proto nesmí být důkazem zátěžové připravenosti celých B–D.

## Existující cílené testy a mezery

| Oblast | Co už existuje | Co v dlouhém A-chaosu ani nalezených cílených testech není dlouhodobě zatížené |
|---|---|---|
| B3/B4/B6 reviews | `tests/test_reviews.py:106` trvalý strop kol po znovuotevření Store; `:212` souběžné verdikty a submissions; security scan a přesný commit v dalších testech | Opakované souběhy mnoha review se sweepy a přechody ostatních modulů ve stejné DB, růst fronty/historie, skutečný restart mezi navazujícími moduly |
| C1/C2 fix tasks/loop | `test_fix_tasks.py:76` politika a lidské schválení, `:165` opakovaná kontrola před startem; `test_fix_loop.py:33/:52` navázání review a strop kol; `test_fix_runner.py` skutečný sandbox nad syntetickým repo, cancel, test fail-before/pass-after, kontrola publikace | Opakované závody schválení/revokace/startu a vznik dalšího kola opravy při současném verdictu/restartu. Samotné volání fix_record_result ve stavovém testu není spuštěná oprava |
| D3–D7 research | `test_research.py:96` 4 souběžné starty proti limitu2; `:115` jediná syntéza, `:148` obnova Store+sweep po deadline; cancel, cutoff, budget a neznámá spotřeba; profil/probe/broker oprávnění a bridge v dalších případech | Dlouhodobé prokládání completion/cancel/deadline/budget a duplicitních sweepů se soupeřícími operacemi dalších modulů. Reálná síťová rešerše/poskytovatel není součástí těchto syntetických zkoušek |
| B7 quality queue | `test_quality_queue.py:21` dedupe přes agent/repo/commit/base/policy, `:34` restart/nevykonat running znovu, `:46` pause/policy change; navázání Stop hook/review | Opakovaná souběžná deduplikace, zaplnění limitu20, dojezd a restart při smíšené DB zátěži. Stavová fronta sama neprokazuje skutečné provedení nástrojů |
| B7 skutečný runner | `test_quality_runner_e2e.py:29` opt-in: Python+TS, PASS/FAIL/REVIEW, namespace/odříznutí prostředí a sítě, cancel/pauza, potomci, broker fronta. Konkrétní scénáře nejsou chybějící | Delší smíšený soak. Standardní CI (`.github/workflows/ci.yml`) nastavuje jen pytest -q, opt-in proměnnou nenastavuje, tedy tyto E2E automaticky nepokrývá. 30 SKIP nelze vydávat za PASS |

`test_init_and_stress.py:68` má contention běžných leases (40 tasků/8 vláken), `:90` claims10 vláken. To není zátěž B–D. Prohledány versionované tests, scripts a workflow; samostatný dlouhý smíšený soak B–D jsem nenašel. Nevylučuji neversionované externí experimenty.

## Nejmenší doporučené doplnění — po domluvě s Adou

**První krok: 10 minut stavového soak + nejvýš60s dojezd**, samostatný malý opt-in harness. Jeden proces, dvě vlákna s vlastními Store instancemi proti jedné nové DB. Celkově nejvýš2 operace/s, žádný busy loop. Nízká priorita nice19, paměťový limit512MiB; odmítnout start, pokud preflight/prostředky nejdou ověřit. 2min smoke před10min, ne další8h. Pevný seed a deterministicky vynucené scénáře, ne naděje, že je náhoda někdy trefí.

Použít již existující fixture/patterny z uvedených testů a public Store přechody. Jeden malý git repo s několika předem vytvořenými neškodnými commity, unikátní topic/id pro každou sadu operací. Nepřidávat nový orchestrátor či systém služeb. Koordinaci závodů udělat bariérami/eventy s timeoutem. Výjimky vláken musí zneplatnit výsledek, ne jen skončit na stderr.

Čtyři rodiny scénářů prokládat v téže DB:

1. **Reviews:** otevřít, souběžné verdikty (jeden vítěz), nový commit zneplatní approval, dojet k eskalaci. Nepřekročit uložený počet kol, neztratit audit.
2. **Fix state:** schválení/revokace versus lease/start; bez platné politiky a přesného schválení nikdy STARTED. Navazující verdict vytvoří právě jedno další kolo, nejvýš původní+3 opravy, všem novým taskům chybí nové lidské schválení. Výsledek opravy bude syntetický stav, žádný agent nebude zapisovat repo.
3. **Research state:** dva projekty nejvýš, třetí start odmítnut; souběh dokončení poslední části se sweep/cancel. Právě jedna syntéza na research, žádné znovuotevření terminálního stavu, žádné přijetí pozdního výsledku přes cutoff, další start se neprovede při vyčerpaném/unknown usage. Spotřebu vkládat přes existující důvěryhodnou testovací cestu a vázat na přesný pokus.
4. **Quality queue:** opakované stejné žádanky obou vláken pod stejnou identitou → jedno id; různé identity se podle kontraktu neslučují. Naplnit20 rozpracovaných žádostí, další odlišnou odmítnout; policy change/pause zablokují vykonání, running po recovery → interrupted a nikdy slepě znovu. Syntetický collector smí logovat pokus/odmítnutí; nesmí vyrábět falešný skutečný quality run/PASS.

Každých30s znovuvytvořit Store instance a pokračovat nad stejnou DB. **To je obnova aplikačního stavu, nikoli SIGKILL brokeru.** Přidání skutečného restartu izolovaného brokeru mezi moduly je druhý malý experiment po zeleném stavovém smoke; nebude implicitně vydávané za splněné. Jednotlivé deterministické crashpointy cílit mezi verdict→další fix task a výsledky částí→syntéza; nedokončená navazující práce musí být viditelně zadržená nebo obnovitelná, ne tiše ztracená.

## Co musí obsahovat výsledek

- Exact SHA zdroje, seed, skutečná délka a počty úspěšných/odmítnutých přechodů každé rodiny. Nejméně20 dokončených scénářů pro každou rodinu a alespoň jeden doklad každého vynuceného závodu, jinak INCOMPLETE.
- Dedupe, strop kol/rozpočtu/queue a neměnnost terminálních stavů kontrolovat průběžně, nikoli jen posledních30/500 položek API.
- Na konci kontrola všech relevantních řádků SQL: integrity_check=ok, foreign_key_check bez nálezů (doplněk, ne náhrada kontrol vazeb); explicitní kontroly odkazů review↔fix, research↔parts/synth, quality↔run. Žádný running/leased/started bez vysvětleného držitele; záměrně držené awaiting_review/needs_review/escalated/interrupted nejsou chyba dojezdu.
- Zaznamenat SQLite lock/timeouts, nejdelší latenci, peak RSS a velikost DB. Každá nevysvětlená výjimka, timeout nebo chybějící scénář znamená FAIL/INCOMPLETE. Neprezentovat pomalost jako problém podle předem nevysloveného produkčního SLA.

## Hranice a návaznost

- Krátká nová root cesta (unix sockety<100b), vlastní HOME/DB/operator policies, žádná kopie produkční DB ani konfigurace. Žádné skutečné přihlašovací údaje. PATH s blokujícími stubs codex/claude; pokus spustit skutečného poskytovatele = FAIL. Žádný model/API, instalace, systemd změna nebo produkční zápis. Případné zapnutí syntetických politik/režimů se týká výhradně nové DB, nelze odemykat produkční MODE_LOCKS.
- Neměnit ani přerušovat Adin čistý snapshot8h běhu. Po domluvě implementovat/vykonat jen malý stavový smoke; při tlaku na RAM/CPU ho odložit, ne zmenšovat Adin test.
- **Nejde o akceptaci fix sandboxu, skutečného B7 runneru ani research klientského profilu.** Ty mají své existující cílené E2E testy. Jejich krátké opakování v řádně izolované a omezené scope plánovat odděleně až podle prostředků a domluvy; bez globálních instalací či změny živých služeb.
- Výsledek stavového soak neuzavírá celý plán B–D a neodemyká funkce. Provozní aktivace a lidská akceptace jsou samostatné.

Review #34 bylo již vráceno k opravě jako #1669 (headless exec falešně označen za novou uživatelskou session). Implementace nového soak je návrh; čeká na koordinaci s Adou, nikoli na další potvrzení Michala pro tento read-only průzkum.

## Realizace po schválení #1678

Ada návrh výslovně schválila. Implementace je `tests/soak_bd.py`, malá self-check sada `tests/test_bd_soak.py`; vyžaduje existující projektové dev prostředí (znovupoužívá fixtures, nic neinstaluje). Příklad z kořene čisté pracovní kopie:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/soak_bd.py /tmp/bd-smoke --seconds 120
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/soak_bd.py /tmp/bd-soak --seconds 600
```

Kořen musí být nový, absolutní, krátký. Skript nastaví nice19 a RLIMIT_AS512MiB, zahodí zděděné prostředí a vytvoří vlastní HOME/DB. Dvě workload volání mohou začít současně, ale celkem nejvýš dvě v klouzavé sekundě; setup fixture a SQL kontroly se do této frekvence nepočítají. Při ztrátě odezvy ukončí proces SIGALRM nejpozději po požadované době+60s. Chybějící result.json nebo nenulový exit znamená neúspěch, nikdy implicitní PASS. Spouštět s aktivními assertions (bez -O).

Rodiny scénářů se prokládají po blocích nad jednou rostoucí DB; dvojvláknové závody probíhají uvnitř review, quality deduplikace a research sweepu. **Nejde o všechny možné souběhy mezi různými rodinami.** Fix politika se kontrolovaně vypne před lease a nové opravné kolo čeká na nové schválení. Research rotuje cancel/success/budget/unknown usage/cutoff; předem se ověří limit2 výzkumů a20 quality žádostí. Quality recovery používá explicitní syntetický running marker, nespouští projekt. Po vytvoření dvojice nových Store instancí se znovu ověřují vazby a stavy. Zadržený fix needs_review a review awaiting_review/escalated zůstávají legitimně zachované.

Oproti širšímu návrhu se **neprovádí skutečný pád brokeru, crashpoint mezi verdict a dalším fix taskem, pozdní completion závod ani testování klientského sandboxu**. Jde o malý první stavový soak; cílené E2E z mapy výše jej doplňují. Resource údaj je adresový prostor aktuálního procesu a peak RSS procesu, nikoli změřená suma OS paměti všech krátkých git setup potomků. Žádné závislosti/provozní politiky/služby se nemění.

Výstup v novém kořeni: `result.json`, syntetická DB a fixture repo. JSON uvádí zdrojový commit, hash spouštěného skriptu, skutečnou dobu, počty scénářů, nejdelší měřené sekvenční workload volání a peak RSS. Souhrnné10min PASS vyžaduje20 scénářů každé rodiny;2min smoke pouze alespoň jeden každé. Výsledek samotný není akceptace či aktivace celé fáze B–D.
