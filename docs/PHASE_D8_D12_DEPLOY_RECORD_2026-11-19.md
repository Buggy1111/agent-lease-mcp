# Nasazení D8–D12 (`fcef18d`) — 19. 11. 2026, Ada

Michal 19. 11.: „ano máš muj souhlas“. Review #31 schválil Nox na `6493d66` (security pass), merge `fcef18d`, 984 passed.

| Krok | Skutečný výsledek |
| --- | --- |
| Runtime | `runtime-fcef18d`: interpret zkopírován z `runtime-4996d89` (bajtově shodný), nový venv, závislosti s omezením na `pip freeze` starého runtime, `pip check` bez chyb. |
| Artefakt | Wheel z `git archive fcef18d`, SHA-256 `9e72aa571fdecada7e52377b2c8edb90c87d5e1f20a49129f9ba6edff41008c4`; všech 54 Python souborů balíčku bajtově shodných se zdrojem. |
| Zkouška před přepnutím | Dočasný broker nad kopií ostré DB: `mode` a `research_list` odpovídají; `self-audit`, `efficiency` a `health` z nového runtime běží (žádná změna schématu). |
| Okno údržby | 0 doručenek leased/started. Zastaveny bridge, codex-wake, broker; `fuser` bez držitele. |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261119-ada-fcef18d` (0700): DB (integrita `ok`), snímek fronty, unit soubory, původní `current` (`runtime-4996d89`). |
| Ověření | Všechny 4 služby active na `runtime-fcef18d`, integrita `ok`, **88/88 řádků nedokončené fronty shodných**, `doctor`: „Looks good“ (známá varování). Web na 8765 restartován na nový runtime. |

První ostrý `efficiency` ukázal 30 dokončených úkolů bez změřeného finálního pokusu. Jsou to starší úkoly z doby před měřením C3, takže je to očekávané.

**Rollback:** zastavit služby, atomicky vrátit `current` na `runtime-4996d89`, spustit, ověřit interpretery, integritu a frontu.
