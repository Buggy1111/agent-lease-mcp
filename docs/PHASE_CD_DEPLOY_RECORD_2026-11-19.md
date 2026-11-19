# Nasazení fáze C + D1 + D2 (`11c547b`) — 19. 11. 2026, Ada

Michal 19. 11.: „ano souhlasím a pokračujte“. Nasazen přesně `11c547b` (main). Review #23 (D1), #24 (D2), #25 (doctor) a #26 (sloučení C) schválil Nox; opravu P2 ve fázi C schválil nezávislý subagent s Michalovým souhlasem.

| Krok | Skutečný výsledek |
| --- | --- |
| Runtime | `runtime-11c547b`: zkopírovaný stejný standalone Python 3.13 z `runtime-120a20c` (interpret bajtově shodný), nový venv, závislosti s omezením na `pip freeze` starého runtime, `pip check` bez chyb. Od starého se liší jen chybějícími vývojovými nástroji (pytest, ruff). |
| Artefakt | Wheel z `git archive 11c547b`, SHA-256 `4eeaf0bc9e55b79bab33708452af70f79328dc3f447e0c18c409079a281fbd9b`; všech 50 Python souborů balíčku bajtově shodných se zdrojem. |
| Zkouška před přepnutím | Dočasný broker nad kopií ostré DB: migrace (`knowledge` + FTS5, `task_usage`, `fix_tasks`, `fix_loops`) proběhla, integrita `ok`; režim `intelligent`, paměť zapnutá, autor nesmí potvrdit vlastní záznam, `set_mode` / `knowledge_human_*` / `knowledge_list` přes broker nedostupné. |
| Okno údržby | Žádná doručenka leased/started. Bez nouzové pauzy (nic neběželo). Zastaveny oba bridge a codex-wake, pak broker. `fuser room.db`: žádný držitel. |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261119-ada-11c547b` (0700): konzistentní záloha DB přes SQLite backup API (integrita `ok`), drop-iny služeb, původní cíl `current` (`runtime-120a20c`). |
| Přepnutí | Atomicky `current.new` → `current` na `runtime-11c547b`. |
| Ověření | Broker, oba bridge a codex-wake active, `/proc/PID/exe` míří do `runtime-11c547b`. Integrita DB `ok`, **všech 41 řádků nedokončené fronty shodných** se zálohou, pauza vypnutá jako předtím. Ostrý broker vrací `mode` a `knowledge_search`. `doctor`: „Looks good“ (známá varování: WSL restart, 5 needs review, Jev). |

**Poznámka:** autonomní režim zůstává zamčený (`MODE_LOCKS`); text zámku ještě říká, že fáze C není nasazená — odemčení je Michalovo rozhodnutí a změna kódu s review.

**Zbývá:** interaktivní MCP relace (Ada, Nox) uvidí nový nástroj `knowledge` po `/mcp` reconnect nebo nové relaci.

**Rollback:** zastavit bridge, codex-wake a broker, atomicky vrátit `current` na `runtime-120a20c`, spustit služby, ověřit interpretery, integritu a frontu. Nové tabulky starý kód ignoruje. DB neobnovovat za běhu; záloha je výše.
