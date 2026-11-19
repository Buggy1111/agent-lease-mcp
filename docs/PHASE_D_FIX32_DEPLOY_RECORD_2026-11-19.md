# Nasazení `0175752` (oprava #32 + dokumentace D13) — 19. 11. 2026, Ada

Michal 19. 11.: „pak tobě i noxovi už dáme aktuální agent lease“. Kódová změna proti `fcef18d` je jen `broker.py` (Noxova oprava #32: `open_store` počká přes výměnu socketu při restartu brokeru). Review #32 schválila Ada, CI 44595252248 zelené (Python 3.11/3.12/3.13).

| Krok | Výsledek |
| --- | --- |
| Runtime | `runtime-0175752`, interpret shodný s `runtime-fcef18d`, `pip check` OK |
| Artefakt | wheel SHA-256 `0ac3a4b9715649a3a70feebbdae01a3d327b1ebe8b49d624bcf50dca969debf8`, 54/54 souborů shodných se zdrojem; `tests/test_broker.py` 33 passed |
| Zkouška | dočasný broker nad kopií DB: `mode`, `research_list` OK |
| Okno | 0 běžících doručenek; služby zastaveny, `fuser` bez držitele |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261119-ada-0175752` (DB integrita `ok`, fronta, unit soubory, předchozí `runtime-fcef18d`) |
| Ověření | 4 služby active na `runtime-0175752`, integrita `ok`, **104/104 řádků fronty shodných**, `doctor` „Looks good“ (známá varování) |

Interaktivní relace Ady a Noxe drží svůj MCP proces ze startu relace; nový kód dostanou po `/mcp` reconnect nebo v nové relaci.

**Rollback:** zastavit služby, `current` atomicky zpět na `runtime-fcef18d`, spustit, ověřit.
