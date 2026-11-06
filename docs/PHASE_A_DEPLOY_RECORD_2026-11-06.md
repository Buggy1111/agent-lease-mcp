# Fáze A: záznam provozního nasazení (6. 11. 2026, 04:36–04:41)

Michal dal výslovný souhlas 6. 11. ve 4:3x („Nasadit teď“). Provedla Ada podle `PHASE_A_DEPLOY_PLAN.md`.

| Krok | Výsledek |
|---|---|
| 0 Runtime | `~/.local/share/agent-lease/runtime-b483265/`: CPython 3.13.18 (archiv SHA-256 `126343c5…81be` = uložený manifest), venv podle `requirements.lock`, balíček z `b483265` (non-editable), soubor `IDENTITY`. Shebangy ukazují do `runtime-b483265`. Plná sada v runtime **499 passed, 1 xfailed**. `verify_sqlite_runtime.py`: MCP (18 nástrojů), broker, bridge i worker SQLite 3.53.1, záloha a integrita ok (`runtime-check/`). |
| 1 Okno | Žádné `leased/started`. 15 pending, 5 needs_review. Pending úkoly pro codex (#573–586) už interaktivně potvrzené, bridge je kvůli `skip_accepted` nevezme. |
| 2 Stop | Všechny 4 služby zastaveny. Webové rozhraní neběželo. `room.db` neměl nikdo trvale otevřený. |
| 3 Záloha | `/home/buggy1111/.local/share/agent-lease/deploy-backup-20261106-043842`: celý `~/.agent-lease` (tgz, SHA-256 v `SHA256`), unit soubory, `~/.claude.json`, `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.codex/hooks.json`, snímek fronty, `previous-current.txt` („nebyl“). Integrita zdroje ok. |
| 3b **Migrace (chyběla v plánu)** | Nový kód odmítá WAL: `python -m agent_lease_mcp.sqlite_migrate --all-clients-stopped` → journal DELETE, integrita před i po ok. Pak `agent-lease stop` (paused=1) a `agent-lease backup`: integrita zdroje i kopie ok. |
| 4 Přepnutí | Atomický symlink `current → runtime-b483265`. Drop-iny `10-runtime.conf` pro broker, `bridge@` (`Delegate=yes`, `--cgroup`) a codex-wake, `daemon-reload`. |
| 5 Klienti | MCP **a hooky (chyběly v plánu)** v `~/.claude.json`, `~/.claude/settings.json`, `~/.codex/config.toml` a `~/.codex/hooks.json` přepnuty z `dev/.venv/bin` na `current/venv/bin`. JSON validní. |
| 6 Start | Všechny 4 služby active. `/proc/PID/exe` = `runtime-b483265/python/bin/python3.13`, cmdline `current/venv/bin/…`. Bridge ve vlastních cgroup `…/agent-lease-bridge@{claude-code,codex}.service`, `Delegate=yes`. `doctor` (absolutně z `current`): SQLite 3.53.1, hooky a identity ok. |
| 7 Kontrola | Integrita ok. Fronta = snímek kromě #159/#160 (staré zprávy typu result z října), které broker smazal podle pravidel uchovávání. Webové rozhraní spuštěno jako `agent-lease-webui-deploy` (přímo `agent-lease-webui`; `agent-lease web` odmítá spuštění bez agentní identity). **Zrušení stopu provádí Michal ve webovém rozhraní.** |

## Dokončení (6. 11., 04:45–05:25)

| Krok | Výsledek |
|---|---|
| Restart relací | Michal restartoval Claude Code i Codex. Nové `agent-lease-mcp` (Claude pid 2715020, Codex pid 2707979) mají `/proc/PID/exe` = `runtime-b483265/python/bin/python3.13`. |
| Zrušení stopu | Webové rozhraní mělo chybu: živá aktualizace neposílala stav pauzy, takže tlačítko „Resume“ se hned přepsalo na „Stop“. Michal tím ve 04:50 omylem pauzu zapnul znovu. Opraveno v `0d4fd13` (regresní test), web předělán v `7a55579`. Ostrý web dočasně běží z repa jako transient `agent-lease-webui-dev`. Michal stop zrušil ~05:19. |
| 8 Živý test | **PASS** oběma směry. `michal → claude-code` #639: bridge převzal, headless běh, výsledek #645. `claude-code → codex` #646: bridge převzal, výsledek #650 „PHASE_A_LIVE_CODEX_OK“. Michal 6. 11. potvrdil pokračování podle plánu. |

Nálezy z živého testu (do fáze B):
- Headless worker běží v plan mode a nemůže volat nástroje agent-lease; Stop hook ho přesto upomíná o cizí nepotvrzené zprávě (#642 patřila interaktivní relaci). Upomínky v headless běhu by měly být potlačené nebo omezené na vlastní úkol.
- Headless worker vycházel ze zastaralé paměti („necommitnuté změny run_fence“), přestože strom byl čistý. Worker má stav repa ověřovat, ne přebírat z paměti.

## Zbývá
- A11 (restart Windows/WSL) – ruční test s Michalem podle `PHASE_A_PREDEPLOY_REVIEW.md`.
- Vrátit web z repa na připnutý runtime při příštím sestavení runtime.

## Poznámky
- `codex-wake` hlásí víc Codex relací se jménem „nox“. Starší problém konfigurace, nesouvisí s nasazením.
- Návrat zpět: `PHASE_A_DEPLOY_PLAN.md`, sekce Návrat zpět. Zálohy v `/home/buggy1111/.local/share/agent-lease/deploy-backup-20261106-043842`. Neověřeno: jak se starý kód (uv tool) zachová k DB v režimu DELETE. Při návratu nejdřív otestovat na kopii zálohy.
