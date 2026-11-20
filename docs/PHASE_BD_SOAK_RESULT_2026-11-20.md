# B–D Store-state soak — 20. 11. 2026

Nox, zadání #1670 a schválení rozsahu Adou #1678 v rámci Michalova zadání zátěžových testů. Větev `nox/bd-soak-20261120`, základ `5e8a171caab67fdcf30845fd7cdd093a4f6bf2aa`. Žádné nasazení, instalace, push nebo změna produkční konfigurace/služeb.

## Předmět a hranice

Opakované prokládání review, fix stavu, research a quality žádostí ve stejné nové rostoucí SQLite DB. Dvouúčastnické závody jsou uvnitř review verdiktů, research sweepu a quality deduplikace. Nejde o všechny souběhy mezi různými rodinami. Store instance se obnovují za běhu; **není to pád/restart brokeru**. Fix výsledek je syntetický, quality recovery má syntetický running marker; **nespouští se agent, sandbox runner ani skutečná quality kontrola**. V testovací DB nesmí vzniknout žádný quality run/PASS.

Ochrany: vlastní nový HOME/DB/politiky a neškodné fixture repo, zahodit zděděné prostředí, blokující codex/claude stubs, nice19, RLIMIT_AS512MiB pro proces, nejvýš2 workload Store calls v klouzavé sekundě (dvojice smí závodit). Setup fixture, vynucené SQL fault markery a SQL kontroly jsou mimo tuto frekvenci. Hard SIGALRM po době+60s. Žádný skutečný provider, síťový model ani produkční DB.

## Výsledky

- Self-check `tests/test_bd_soak.py`: **4 PASS**. Ověřují omezení frekvence, předání chyby workeru, všechny varianty smíšených scénářů a odmítnutí neexistujícího quality výsledku.
- Ruff změněných Python souborů: PASS. Plná regresní sada nebyla opakována, aby se nekřížila s běžícím Adiným8h chaosem; produkční kód se nemění.
- Smoke120s: **PASS**, skutečně125,33s, 6 scénářů každé rodiny, 3 Store reopens, peak RSS44 768KiB (~43,7MiB), DB323 584B, nejdelší měřené sekvenční volání45,94ms. Metadata: `test-results/bd-soak-20261120/smoke.json`, log vedle něj. Spuštěn pracovní skript nad základním commitem, hash skriptu `11f645cdb16a557f9de57d5d7050f5f66733ac88cc7c31b584ffecef72a379da`.
- Soak600s: **PASS, exit0**, skutečně606,53s. **31 scénářů každé ze4 rodin, 18 Store reopens**, capacity check1; research cancel7/budget6/unknown6/cutoff6. Peak RSS45 104KiB (~44,0MiB), DB606 208B, nejdelší měřené sekvenční volání123,94ms. Spuštěn přesný commit `aa26c05cd13ffa95ad0cd05cd050612d2265638c`, hash skriptu shodný se smoke i s konečným souborem. Metadata: `test-results/bd-soak-20261120/soak.json`, průběžný log vedle něj. Následný evidence commit mění jen dokumentaci/logy, nikoli testovaný kód.

Smokem prošly capacity20 quality/2 research, review souběhy+strop3 kol, fix politika a nové schválení dalšího kola, research cancel/success/budget/unknown usage/cutoff a jediná syntéza, quality dedupe/policy refusal/interrupted bez replay. Každá dokončená sada kontrolovala všechny relevantní řádky (žádný limit30/500), SQLite integritu a vazby. Legitimní needs_review, awaiting_review, escalated a interrupted nejsou chyba dojezdu.

## Co výsledek nedokazuje

Neprokazuje zátěž skutečného research klienta, fix sandboxu, B7 project runneru, webu ani broker crash recovery. Review revize mají syntetické40znakové hodnoty; brokerové ověření existence Git objektu ani skutečný secret scanner se touto Store cestou nezatěžují. Nebyly injektované pády mezi review verdict a vytvořením dalšího fix tasku ani závod pozdního výsledku přes cutoff. Tyto mezery zůstávají popsané v návrhu `PHASE_BD_SOAK_PROPOSAL_2026-11-20.md`; existující cílené runner E2E jsou samostatné. Výsledek neaktivuje B7/C/D3–D7 a neuzavírá celý plán A–D. Adin současný8h chaos má vlastní snapshot a závěr, zde se nepředjímá.

Peak RSS je měření hlavního procesu; limit adresového prostoru není měřením součtu paměti všech krátkých git setup potomků. Latence nevyjadřuje produkční SLA ani výkon poskytovatele. Syntetická data zůstala v `/tmp/bd20-smoke` a `/tmp/bd20-soak`, verzované jsou jen skript/dokumentace/logy/JSON, ne databáze nebo osobní konfigurace.
