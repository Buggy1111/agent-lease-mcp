# Nasazení D3–D7 výzkumný tým + P3 opravy (`4996d89`) — 19. 11. 2026, Ada

Michal 19. 11.: „ano, jed dalju“ (odpověď na: schválit review #30, sloučit do `main`, nasadit). Review #30 bylo po 3 kolech eskalováno; Nox potvrdil poslední opravu `37e8963`, Michal schválil. Navíc `e2a76ab` (jen text zámku Autonomního režimu, mimo #30). Obsahuje i dosud nenasazené P3 opravy z `a65ebe0` (#27, #28).

| Krok | Skutečný výsledek |
| --- | --- |
| Testy na `4996d89` | 974 passed, 30 skipped, 1 xfailed; ruff čistý. |
| Runtime | `runtime-4996d89`: interpret zkopírován z `runtime-11c547b` (bajtově shodný), nový venv, závislosti s omezením na `pip freeze` starého runtime, `pip check` bez chyb. |
| Artefakt | Wheel z `git archive 4996d89`, SHA-256 `db6ee2dfcd91a4bba8964ed106aefac01b26c1e22fee1dea95762819e7699374`; všech 52 Python souborů balíčku bajtově shodných se zdrojem. |
| Zkouška před přepnutím | Dočasný broker nad kopií ostré DB: migrace `research`, `research_msgs`, `research_attempts`, integrita `ok`; `research_list` funguje; `research_start` bez `research.toml` odmítnut; `lease_next(research=True)` z cizího procesu odmítnut („only for the agent's bridge service“). |
| Okno údržby | Žádná doručenka leased/started. Zastaveny oba bridge a codex-wake, pak broker; `fuser room.db` bez držitele. |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261119-ada-4996d89` (0700): DB přes SQLite backup API (integrita `ok`), snímek nedokončené fronty, unit soubory, původní cíl `current` (`runtime-11c547b`). |
| Přepnutí | Atomicky `current.new` → `current` na `runtime-4996d89`. |
| Ověření | Broker, oba bridge a codex-wake active, `/proc/PID/exe` v `runtime-4996d89`. Integrita `ok`, **76/76 řádků nedokončené fronty shodných** se zálohou. `doctor`: „Looks good“ (známá varování: WSL restart, 5 needs review, Jev). |

**Výzkum zatím neběží** (záměrně): chybí `research.toml` i potvrzení sondy. Operátor (Michal):

```sh
agent-lease research-probe --agent claude-code      # zapíše research-probe.json při PASS
# ~/.agent-lease/research.toml (0600):
#   version = 1
#   enabled = true
#   agents = ["claude-code"]
#   budget_tokens = <mez na jeden výzkum>
```

Autonomní režim zůstává zamčený (`MODE_LOCKS`), odemčení je Michalovo rozhodnutí.

**Rollback:** zastavit bridge, codex-wake a broker, atomicky vrátit `current` na `runtime-11c547b`, spustit služby, ověřit interpretery, integritu a frontu. Nové tabulky starý kód ignoruje. DB neobnovovat za běhu.
