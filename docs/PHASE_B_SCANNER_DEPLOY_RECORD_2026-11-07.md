# Nasazení scanneru 9134485 — 7. 11. 2026, Nox

Michal přímo schválil konkrétní připravený `READY.md` a nasazení `9134485371fdd6d1c92c16562aa04d188c3f2125` s následným omezeným B7.0 pilotem podle [plánu nasazení](PHASE_A_DEPLOY_PLAN.md). Zdrojový main nebyl měněn, žádný push ani merge. Audit #8 již dříve formálně schválen; scanner nezávisle obsahově APPROVE, 592 passed / 1 xfailed, 52 nezávislých sond PASS. Review #9 v brokeru zůstává awaiting, aby nevzniklo zakázané oznámení Adě; tento záznam to nezakrývá.

| Krok | Skutečný výsledek |
| --- | --- |
| Příprava | `runtime-9134485` vznikl přímo ve finální cestě. Zkopírována stejná standalone distribuce Python 3.13.18 / SQLite 3.53.1 z ověřeného předchůdce; hash interpretu shodný. Všech 75 verzí závislostí shodných, `uv pip check` PASS. Nový pilotní coverage/lizard není v runtime. |
| Artefakt | Wheel SHA-256 `90b6f3acd3d4e5288bf15f1e55fee21f3f4a66b2cd7954cc6985bc5f127ad0e4`; 40 Python souborů bajtově shodných s kandidátem. MIT licence a metadata ověřeny ve wheel/sdist. |
| Runtime před přepnutím | Kontrola přímo z finální cesty: syntetický broker/bridge/worker, všech 20 MCP nástrojů, DB a záloha integrita `ok`. Entry point shebangy míří do finálního venv. |
| Okno údržby | Globální board neměl leased/started. Standardní MCP emergency stop potvrzen. Zastaven broker, oba bridge, wake a web; zastaveny dva staré Agent Lease MCP procesy. `fuser room.db` potvrdil nulové držitele. |
| Soukromá záloha | `/home/buggy1111/.local/share/agent-lease/deploy-backup-20261107-nox-9134485`, adresář 0700. Kopie adresáře místnosti bez socketů, drop-iny, stávající konfigurace klientů/environment, původní `current`, CLI konzistentní backup a snímek fronty. Hodnoty tokenů/configurací nebyly vypisovány ani odesílány. |
| Integrita | Zdroj i kopie DB `ok`; SHA-256 offline `room/room.db`: `780a0176b26cf4bee6f4fe1690725a7a9a98821d83d869e8e90514e1e620972f`. |
| Přepnutí | Atomické `current.new` → `current`, cíl `runtime-9134485`. Při chybě startu byl připraven automatický návrat na původní `runtime-eb3a4f5`; nebyl potřeba. Stávající čtyři služby znovu spuštěny. |
| Web | Původní transientní `agent-lease-webui-deploy.service` po stop zanikl. Obnoven přes stejný current entry point a existující environment soubor; default loopback web. Agentí konfigurace ani routing se neupravovaly. |
| Ověření po startu | Všech pět služeb active; skutečné `/proc/PID/exe` míří do `runtime-9134485/python/bin/python3.13`, bridge `Delegate=yes` a správná cgroup. Integrita DB `ok`, **všech 101 řádků nedokončené fronty přesně shodných** s offline snímkem, pauza zůstala zapnutá. |

[Provozní důkaz](test-results/b7-0-9134485/deploy-verification.json), [finální syntetická kontrola runtime](test-results/b7-0-9134485/final-runtime-check.json), [hash artefaktů](test-results/b7-0-9134485/build-checks.json), [výsledky následného pilotu](research/2026-11-07-b7-0-pilot.md).

Zrušení pauzy je lidská akce ve webu; nebylo automatizováno. Pro MCP je potřeba nová interaktivní relace/restart připojení, staré procesy nebyly ponechány na předchozím kódu. Živý úkol Adě se neposílal. Konfigurace agentů, routing, oprávnění ani systemd drop-iny se neměnily.

Rollback podle schváleného plánu: ponechat pause, zastavit stejné služby a klienty DB, atomicky vrátit uložený cíl `current` na `runtime-eb3a4f5`, obnovit původní drop-iny pouze pokud by se lišily a spustit služby/web; ověřit interpretery, integritu a frontu. DB neobnovovat za běhu. Záloha obsahuje provozní data a zůstává lokálně; není součástí tohoto commitu.
