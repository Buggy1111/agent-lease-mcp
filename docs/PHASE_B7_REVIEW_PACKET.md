# B7 — kandidát k nezávislému review

## Opravy auditu 11. 11. 2026 — aktuální kandidát

Autor oprav: Nox. Větev `nox/b7-fixes-20261111`, základ oprav `6e9331e`.
Review celého B7 se porovnává s produkčním základem `9134485`; přesný plný hash
kandidáta určuje záznam MCP review, nikoli tento průběžný dokument.
Michal schválil fázi B 11. 11.; opravy a nezávislé schválení B7 tím nejsou nahrazené.
Produkce, služby, hooky a operátorská konfigurace se touto větví nemění.

| Nález | Výsledek |
|---|---|
| F1 — identita nástrojů a konfigurace | Python safe path, ochrana skryté konfigurace, tool modulů/balíčků a node_modules; unit i skutečná sandboxová regrese |
| F2 — základ kritických změn | Povinný plný operátorský SHA v `[policy_bases]`, oddělený od review base; v inspekci i uložené evidenci; navazující commit schválení neobejde |
| F3 — prostředky brokeru | Samostatná cgroup pro každý příkaz: 1 GiB, bez swapu, 128 procesů, 75 % CPU; ověřené limity, watchdog a rezerva pro broker |
| F4 — odmítnutí a opt-out | Ukládají se jako pokusy bez falešných procesních pozorování; dashboard ukazuje aktuální commit a stav spuštění, kritický opt-out zůstává REVIEW |
| F5 — E2E v CI | Nový povinně spouštěný job `quality-e2e` s odděleným omezeným/delegovaným scope; nepodporovaný host selže místo skipu |
| F6 — důvěra v metriky | Výslovně dokumentovaný limit: projektový kód může falšovat coverage/CRAP; čísla jsou poradní signál, nikoli důkaz poctivého provedení testů |
| F7 — secret scan | Broker sám kontroluje přesný Git diff při zápisu evidence; samotný čistý scan od klienta ani stará evidence bez broker scanu nestačí k PASS |
| F8 — opakování požadavku | Odmítnutý/přerušený/obsazený požadavek lze explicitně znovu zařadit; aktivní a dokončené běhy zůstávají deduplikované |
| F9 — SQLite | Chyba při běhu dokončí požadavek jako odmítnutý; selhání finálního zápisu vynutí obnovu fronty po návratu DB, bez automatického opakování práce |

Aktuální ověření:

- Běžná celá sada: **781 passed, 30 skipped, 1 xfailed**. Tento běh předcházel
  posledním doplněným regresím a úpravě filtru UI; následné cílené testy níže
  ověřují dotčené části. Skipy jsou výhradně samostatná sandboxová sada.
- Finální cílená sada quality/broker/webui: **223 passed**; dodatečná kontrola
  identity inspekce samostatně v `tests/test_quality.py`.
- Sandbox: všech **36 různých případů** ověřeno v reálných Python/TypeScript
  prostředích v dočasných scope. Důkazy pocházejí z více běhů: původních 27 případů
  prošlo v úplné sadě; závěrečný výběr 10 případů prošel celý a zahrnuje zbývající
  tři původní případy i šest nových regresí. Nejde o tvrzení o jediném 36/36 běhu.
- Chromium: **31 kontrol PASS**, včetně odmítnutí/opt-outu a escapování diagnostiky.
- Ruff, kontrola whitespace, wheel i sdist: **PASS**; lokální B6 scan celého
  rozsahu od `9134485` i samotných oprav bez nálezů.

První pokusy odhalily nevyhovující lokální Python bez memfd a nespustitelnou kopii
TypeScriptu; runner tyto podmínky nepovažuje za PASS. Použity byly existující
vyhovující nástroje, bez globální instalace. Dvě fixture byly přizpůsobeny nové
ochraně: Python sada musí obsahovat test a změna coverage příkazu vyžaduje lidské
schválení. Test cgroup čeká na projektový proces po důvěryhodném bootstrapu.

Hostované GitHub CI dosud neběželo; větev nebyla pushnuta. Stejný OS uživatel
s přímým přístupem k broker DB, operátorské politice či webovému tokenu zůstává
důvěryhodnou hranicí systému. Oddělený sandbox tato data projektovému kódu nemountuje.
Nezávislé review oprav a případná aktivace jsou samostatné kroky.

Postup aktivace začíná MCP `review(action="check")` pro plný hash kandidáta.
Detaily: [runner](PHASE_B7_2_RUNNER.md), [aktivace](PHASE_B7_ACTIVATION_PLAN.md).

## Historický kandidát 66039c0 — původní stav a důkazy

Následující text zachovává původní předání. Jeho stavové výroky o otevřené fázi B,
kapacitě Ady a chybějícím auditu nahrazuje aktuální oddíl výše.

Autor: Nox. Produkční základ: `9134485371fdd6d1c92c16562aa04d188c3f2125`.
Poslední změna produktového kódu: `e921d8c223dc77d40c1208d3462a6c69577cff1e`.
Kandidát včetně opravené testovací fixture: `66039c0839f8fafc6bfd7437ff0c330dbb60f470`.
Zdrojová pracovní větev: `nox/b7-mvp-20261107`. Kandidát je s přímým souhlasem
Michala přenesen na `main`; produkční runtime zůstává `9134485`.
Fáze B zůstává otevřená do schválení **Adou i Noxem**, až se Adě obnoví kapacita.
Publikace kódu ani tento report chybějící nezávislé schválení nenahrazují.
Tento report není nezávislé schválení vlastního kódu.

## Chování

Na dokončeném commitu běží povolené testy, lint, typecheck a build v izolovaném
offline prostředí. Volitelná coverage a Python CRAP mají skutečné číselné
adaptéry. Výsledky a historie se zobrazují ve stávajícím dashboardu. Stop/review
události umí přes existující broker zařadit deduplikovaný požadavek bez čekání
na drahou kontrolu. Projektové soubory nemohou samy povolit hostitelské nástroje,
nastavit verdikt ani schválit vlastní změnu.

Povinné selhání = FAIL; nízká coverage/vysoké CRAP nebo chybějící schválení =
REVIEW; nepodporovaná metrika = N/A; neověřené prostředí = SETUP REQUIRED.
Kritická změna testů/CI/security/gate navíc potřebuje přímé potvrzení člověka
pro konkrétní run. Žádná výjimka nepřebije selhávající povinný test nebo chybějící
nezávislý bezpečnostní verdict B6.

Kontrakt, omezení a použití: [runner](PHASE_B7_2_RUNNER.md).

## Důkazy

| Kontrola | Výsledek a rozsah |
|---|---|
| Python + TypeScript E2E | [28 prošlo](test-results/b7-2/e2e.xml), 312,67 s; skutečné subprocessy/kompilátor/namespaces, role a review jsou syntetické |
| Parser/evidence/refusal | [95 prošlo](test-results/b7-2/numeric-unit.xml); navíc 4 nové kombinace CRAP prošly samostatně |
| Zamčená DB a recovery fronty | [7 prošlo](test-results/b7-2/queue-recovery.xml); další deterministický test startu s předem zamčenou DB + 6 queue testů prošly |
| Prohlížeč | [30 kontrol](test-results/b7-2/browser-results.json), beze změny frontendu po tomto ověření |
| Ruff | `ruff check src tests` PASS |
| Balíček kandidáta 66039c0 | Offline wheel/sdist sestaveny; všech 46 modulů shodných se zdrojem, LICENSE přítomná, metadata MIT |
| B6 heuristický scan celého diffu | `scan_diff(git diff 9134485 HEAD)` = prázdný seznam; nenahrazuje nezávislé security review |

Finální vlastní Quality Gate kandidáta **66039c0** nad celým základem
**9134485** dokončeno: **tests PASS, lint PASS, build PASS**; celkový **REVIEW**
kvůli chybějícímu nezávislému B6/security a lidskému schválení kritických změn.
[Autoritativní evidence vlastního běhu](test-results/b7-2/own-run.json) obsahuje
procesní metadata a digests, nikoli surový výstup. Typecheck/coverage/CRAP jsou
v tomto vlastním projektu N/A podle jeho konfigurace; skutečné metrické adaptéry
ověřují uvedené Python/TS E2E fixture. E2E se v plné standardní sadě explicitně
přeskakují a mají samostatné výše popsané ověření. Finálních 21 cílených kontrol
metrik rovněž prošlo.

Autoritativní strojový souhrn: [validation.json](test-results/b7-2/validation.json).

První vlastní gate commitu 04f22af správně vykázal FAIL při timeoutu testů po
600 sekundách. Diagnostika odhalila závod obnovy fronty se SQLite zámkem při
startu brokeru: výjimka ukončila obslužné vlákno ještě před `serve_forever`, a
shutdown pak čekal. Commit 4cb7ca8 přesunul obnovu do workeru s retry při dočasné
chybě. Obnova musí uspět před zpracováním jediného queued jobu. Nová regrese
zamkne DB před startem serverového vlákna a ověří dostupnost ping bez DB.
Zastaralé zaseklé testy byly ukončeny; jejich částečné výsledky nejsou PASS.

Druhý vlastní gate 4cb7ca8 dokončil pytest za 228,7 s bez timeoutu; lint/build
prošly, testy správně vykázaly FAIL. Diagnostika určila původní test
`test_crash_in_tick_kills_the_whole_process_group`: nový důvěryhodný PID 1
nereapoval osiřelé ukončené procesy během života hlavního testu, takže bridge
nemohl potvrdit zánik procesní skupiny. Commit e921d8c doplnil waitpid reaper a
skutečný E2E důkaz úklidu orphana ještě před ukončením projektového procesu.
Původní bezpečnostní test bridge není oslaben ani vynechán.
Finální E2E na e921d8c: 29 prošlo, nový TS reaper případ vrátil SETUP REQUIRED
kvůli chybějícímu Python manifestu ve fixture. Commit 66039c0 doplnil pouze
manifest; oba reaper případy poté prošly (3,8 s). Produktový kód se nezměnil.
Všech 30 odlišných případů je ověřených napříč těmito dvěma běhy; celá sada
se po opravě samotné fixture neopakovala. Původní neúspěšné XML je zachované.

## Místa vyžadující review

- `quality_runner.py`, `quality_namespace.py`, `quality_collect.py`: cgroup
  preconditions, přesné Git bloby, tool piny, namespaces, parent-death/descendant
  cleanup, omezení času/výstupu a soukromý metadata FD.
- `_quality_runs.py`: privátní zapisovací kontrakt, opakované ověření plánu,
  exact repo/base/head B6 authority, invalidace schválení, report-only CRAP.
- `_quality_queue.py`, `broker.py`, `lifecycle.py`: recovery před drain, hranice
  restartu, deduplikace, pause/stop, krátká hook odezva a zakázaný import výsledků.
- `webui.py`: přímá lidská role pro policy approval, typ run ID bez koercí,
  escapování a whitelist log URL, oddělené demo a reálné výsledky.

## Otevřené brány fáze B

Nezávislé review tohoto Noxova kódu nebylo provedeno. Adě nebyla poslána žádná
zpráva ani accept/verdict notifikace. E2E s názvy rolí nejsou ostrým během
Claude/Codex klientů. Globální aktivace/hook trust a produkční limity nejsou
aplikované. GitHub main byl při kontrole nechráněný; dashboard merge nevynucuje.
Původní právní/compliance otevřené položky nepovažujeme za automaticky vyřešené.

[Konkrétní aktivace a rollback](PHASE_B7_ACTIVATION_PLAN.md) obsahují kontrolní
body, které potřebují přímý Michalův souhlas podle session AGENTS instrukcí.
Připravené šablony: [operátor policy](../deploy/quality/quality-runner.toml.example)
a [broker limity](../deploy/quality/broker-limits.conf.example). Žádná není
aktivovaná. Další projekty a fáze C čekají na dokončení a rozhodnutí vlastníka.
