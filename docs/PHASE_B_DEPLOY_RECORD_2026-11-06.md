# Fáze B3+B4: záznam provozního nasazení (6. 11. 2026, 13:45–14:00)

Michal dal souhlas 6. 11. odpoledne („máš můj souhlas a pokračujte podle dokumentace“).
Provedla Ada podle `PHASE_A_DEPLOY_PLAN.md`. Kód `fdc39b8` (B3+B4, Nox re-review #660 PASS),
pushnuto na `origin/main`.

| Krok | Výsledek |
|---|---|
| 0 Runtime | `~/.local/share/agent-lease/runtime-fdc39b8/`: stejný archiv CPython 3.13.18 (SHA-256 `126343c5…81be` = manifest), `git archive fdc39b8` → `src/`, venv z `requirements.lock`, balíček non-editable. Závislosti `pip freeze` **shodné** s `runtime-b483265`, `pip check` ok. Shebangy do `runtime-fdc39b8`. Plná sada v runtime **521 passed, 1 xfailed**. `verify_sqlite_runtime.py`: MCP 20 nástrojů, broker/bridge/worker/MCP SQLite 3.53.1, bridge úkol dokončen, integrita ok. |
| 1 Okno | Žádné `leased/started`; 16 pending, 5 needs_review. `emergency_stop` zapnut. |
| 2 Stop | Broker, oba bridge, codex-wake a dočasný web `agent-lease-webui-dev` (běžel z repa) zastaveny. `room.db` nikdo neměl otevřenou. |
| 3 Záloha | `/home/buggy1111/.local/share/agent-lease/deploy-backup-20261106-134853`: `~/.agent-lease` (tgz + SHA256), drop-iny, konfigurace Claude/Codex, `previous-current.txt` = `runtime-b483265`, snímek fronty (21 řádků). Integrita zdroje ok. `agent-lease backup` → integrita i kopie ok. Migrace není potřeba (DB už v režimu DELETE). |
| 4 Přepnutí | Atomický symlink `current → runtime-fdc39b8`. Drop-iny a konfigurace klientů už ukazují na `current/…`, beze změny. |
| 6 Start | Všechny 4 služby active, `/proc/PID/exe` = `runtime-fdc39b8/python/bin/python3.13`, cmdline `current/venv/bin/…`. Oba bridge `Delegate=yes` ve vlastních cgroup. `journalctl` bez varování. |
| 7 Kontrola | Integrita ok, journal DELETE, tabulky `reviews` a `review_events` vytvořené, **fronta shodná se snímkem**. Web znovu jako `agent-lease-webui-deploy` z připnutého runtime (už ne z repa). `doctor`: jen známá varování (WSL restart = A11, 5 starých needs_review, Jev vypnutý). |
| 8 Živý test | #667 na claude-code bridge: jedno dokončení, přesná odpověď, procesy v podskupině bridge. Nouzový stop během #670 ukončil 17 procesů do 3 s, úkol v karanténě (`needs_review`, pak zrušen). Michal stop zrušil ve webu. |
| Relace | Michal restartoval Claude Code i Codex. Nové `agent-lease-mcp` (Codex pid 7467, Claude pid 8276) mají `exe` = `runtime-fdc39b8`. Nástroje `review` a `verdict` jsou k dispozici. |

## Druhé nasazení: runtime-d01bf3e (6. 11., 18:42)

Živý test po prvním nasazení ukázal, že `review` bral repo z adresáře, kde relace začala (8a2413d: parametr `repo`, Nox PASS #678).
Bezpečnostní kontrola a Nox pak našli cesty, kudy by `git` v cizím repu spustil program: clean filtr (3abe2d6), filtr ve vloženém
configu (d8c8859, 5a35e3e), filtr v submodulu a promisor lazy fetch (Nox d01bf3e, review Ady PASS #700).

| Krok | Výsledek |
|---|---|
| Runtime | `runtime-d01bf3e`, stejný archiv CPython a závislosti shodné s `runtime-fdc39b8`. V runtime 529 passed, 1 xfailed; `verify_sqlite_runtime.py` 20 nástrojů MCP, SQLite 3.53.1, integrita ok. |
| Okno | Žádné leased/started (25 pending, 5 needs_review), `emergency_stop`. |
| Záloha | `/home/buggy1111/.local/share/agent-lease/deploy-backup-20261106-184202` (adresář DB, drop-iny, konfigurace klientů, snímek fronty 30 řádků, `previous-current.txt` = `runtime-fdc39b8`), `agent-lease backup` ok. |
| Přepnutí a start | `current → runtime-d01bf3e`, všechny 4 služby z nového runtime, bridge `Delegate=yes`. Integrita ok, fronta shodná. Web `agent-lease-webui-deploy` před nasazením neběžel (příčina nezjištěna), znovu spuštěn z runtime. |

## Návrat zpět

`emergency_stop`, zastavit služby, `ln -sfn runtime-b483265 current.new && mv -T current.new current`,
spustit služby, restartovat relace. Nové tabulky `reviews`/`review_events` starý kód ignoruje.

## Zbývá

- A11 hotovo (restart WSL PASS), viz `PHASE_A_A11_RESULT_2026-11-06.md`; zbývá trvalá unita webu a samostart WSL po přihlášení.
- Dál podle roadmapy: B1, B2, B5, B6.
