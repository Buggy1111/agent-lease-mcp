# Nasazení oprav z auditu 120a20c — 11. 11. 2026, Ada

Michal 11. 11.: „tak nasadíme ne? a pak pokračujeme“, s podmínkou schváleného review. Review #11 (opravy z auditu 11. 11.) schválil Nox na přesném commitu `120a20c391a4adfe2b5f91892a47b44202e61114`, security pass. Nasazen byl přesně tento commit; B7 je v něm obsažené, ale zůstává neaktivní (bez `quality-runner.toml` vrací SETUP REQUIRED).

| Krok | Skutečný výsledek |
| --- | --- |
| Runtime | `runtime-120a20c`: zkopírovaný stejný standalone Python 3.13.18 z `runtime-9134485` (SHA-256 interpretu `0195e34a…` shodný), nový venv, 73 závislostí ve stejných verzích (`pip freeze` shodný až na samotný balíček), `pip check` bez chyb. |
| Artefakt | Wheel z `git archive 120a20c`, SHA-256 `9571ee7e4e67fd83528ee1a6309d4d95fd37c19594fc9493d9e4d06bb888dcc7`; všech 46 Python souborů nainstalovaného balíčku bajtově shodných se zdrojem. |
| Zkouška před přepnutím | Dočasný broker a DB: sdílený nájem dvou session přežije Stop první z nich, `prune` není v METHODS, převzetí úkolu a záloha (integrita `ok`); guard vezme nájem `auto (running script)` na skript spuštěný po `cd`. |
| Okno údržby | Na boardu žádný úkol leased/started. Nouzová pauza přes MCP. Zastaven broker, oba bridge, codex-wake a web. `fuser room.db`: žádný držitel. Interaktivní MCP procesy se nezastavovaly (DB přes ně nedrželo nic). |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261111-ada-120a20c` (0700): kopie místnosti bez socketu, konzistentní záloha DB přes SQLite backup API, drop-iny služeb, původní cíl `current` (`runtime-9134485`), snímek nedokončené fronty. Integrita zdroje i kopie `ok`; SHA-256 `room.db` před přepnutím `58f2d1cc…634bc`. |
| Přepnutí | Atomicky `current.new` → `current` na `runtime-120a20c`. Hooky obou klientů míří na `current/venv/bin`, takže broker a hooky se přepnuly naráz (nutné kvůli `edit_gate(session=…)` a `release_session`). |
| Web | Znovu jako přechodná jednotka `agent-lease-webui-deploy` (`systemd-run --user`, UMask 0077) ze stejného `current`. |
| Ověření | Všech pět služeb active, `/proc/PID/exe` míří do `runtime-120a20c/python/bin/python3.13`. Integrita DB `ok`, **všech 51 řádků nedokončené fronty shodných** se snímkem, migrace `claims.session` proběhla, pauza zůstala zapnutá. Nový `doctor` hlásí, že matcher hooku Claude Code postrádá `MultiEdit\|Bash` (známý nález 1, oprava je ruční změna konfigurace). |

Zdroj v `main`: sloučení `72c3b79`, kód (`src`, `tests`) shodný s nasazeným commitem.

**Zbývá člověku:** uvolnit pauzu ve webu. Interaktivní MCP relace (Ada, Nox) běží do restartu na starém kódu; s novým brokerem jsou kompatibilní, nové nástroje uvidí po `/mcp` reconnect nebo nové relaci.

**Rollback:** ponechat pauzu, zastavit stejné služby a web, atomicky vrátit `current` na `runtime-9134485`, spustit služby, ověřit interpretery, integritu a frontu. Přidaný sloupec `claims.session` starý kód ignoruje. DB neobnovovat za běhu; záloha je výše.
