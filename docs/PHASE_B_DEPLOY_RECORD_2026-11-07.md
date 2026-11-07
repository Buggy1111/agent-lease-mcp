# Fáze B: nasazení B1, B2, B5, B6 + nový web (7. 11. 2026, 04:53)

Michal dal souhlas 7. 11. („ano máš můj souhlas“). Provedla Ada podle `PHASE_A_DEPLOY_PLAN.md`.
Kód `eb3a4f5` (main): B1 905c443 (Nox #2), B2 1b625bb (#3), B5 762c8ca (#4), B6 d34f9df (#5),
UI Noxe 6fd6080 (review Ady #6) sloučené s B6 webem, Michalovy docs B7/LICENSE (7a89755).
Výchozí větev na GitHubu přepnuta z `master` na `main`.

| Krok | Výsledek |
|---|---|
| Runtime | `runtime-eb3a4f5`, stejný CPython 3.13.18 (SHA-256 = manifest), závislosti shodné s `runtime-d01bf3e`, `pip check` ok. V runtime 586 passed, 1 xfailed; `verify_sqlite_runtime.py`: MCP 20 nástrojů, SQLite 3.53.1 všude, integrita ok. Sloučený web: 27/27 browser kontrol. Secret scan `d01bf3e..eb3a4f5`: bez nálezu. |
| Okno | Žádné leased/started (73 pending, 5 needs_review), `emergency_stop`. |
| Záloha | `/home/buggy1111/.local/share/agent-lease/deploy-backup-20261107-045259` (adresář DB, drop-iny, konfigurace klientů, `environment`, snímek fronty 78 řádků, `previous-current.txt` = `runtime-d01bf3e`), `agent-lease backup` ok. |
| B5 zapnuto | `AGENT_LEASE_MAX_RUNTIME=3600` v `~/.config/agent-lease/environment`: limit nečinnosti 1800 s (`--timeout`), aktivní úkol nejvýš 1 h. Ověřeno v prostředí procesu bridge. |
| Start | Všechny 4 služby a web z `runtime-eb3a4f5`, bridge `Delegate=yes`. Integrita ok, fronta shodná se snímkem, sloupce B6 v `reviews` (už existovaly, viz níže). |

Poznámka: sloupce B6 (`scan_commit`, `security`…) v provozní DB existovaly už před nasazením, tedy
nějaký proces s novějším kódem se k provozní DB připojil dřív. Migrace je jen přidání sloupců, starý
kód je ignoruje; původ nezjištěn.

## Po nasazení

- Michal zruší pauzu ve webu a restartuje relace Claude Code i Codexu: teprve nové MCP mají `verdict`
  s `security`/`repo` (B6) a review s kontrolou tajemství.
- Návrat zpět: `emergency_stop`, `ln -sfn runtime-d01bf3e current.new && mv -T current.new current`,
  odebrat `AGENT_LEASE_MAX_RUNTIME`, restart služeb a relací.
