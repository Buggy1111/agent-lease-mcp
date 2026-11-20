# Nasazení `79fe207` (doctor #34) — 20. 11. 2026, Ada

Michal 20. 11.: „ano máš můj souhlas". Kódová změna proti `0175752`/`5e8a171` je jen `doctor.py`
(varování, když novější interaktivní relace Codexu nahradila cíl `codex-wake`). Review #34 schválil Nox
na `3d5b5a1`, security pass. Služby samy `doctor` nepoužívají; restart kvůli jednotnému runtime.

Před nasazením (se souhlasem Michala) přepnuto `AGENT_LEASE_CODEX_THREAD` na živou relaci Noxe
`01a1f549-2893-7ff0-9d7e-3679e833e84f`, záloha `~/.config/agent-lease/environment.bak-20261120-thread`.
Příčina: po restartu Codexu 19. 11. 20:29 wake budil starou relaci `01a1dbbd…`; recovery výzva po limitu
v 01:06 tak nedošla a Nox čekal do 04:32.

| Krok | Výsledek |
| --- | --- |
| Runtime | `runtime-79fe207`, interpret zkopírován z `runtime-0175752`, závislosti připnuté na `pip freeze` živého runtime — **shodné**, `pip check` OK |
| Artefakt | wheel SHA-256 `a50f4faeaca1865c56a10c2dfc2aad84f272eb3c200d851a4f60da7aa1893e4a`, 54/54 souborů shodných se zdrojem; proti živému runtime změněn jen `doctor.py` |
| Testy | full 986 PASS / 30 SKIP / 1 XFAIL na větvi; `test_extras` 15 PASS na sloučeném `main` |
| Okno | 0 `leased/started`; služby zastaveny, DB bez držitele |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261120-ada-79fe207` (DB integrita `ok`, fronta, unit soubory, `previous-current` = `runtime-0175752`) |
| Ověření | 4 služby active na `runtime-79fe207` (`/proc/PID/exe`), integrita `ok`, `doctor` „Looks good“ (známá varování), wake ✔ na živou relaci |
| Fronta | 388 → 376 řádků: jen retence při startu Store (`retention_days`) smazala 12 dokončených `expired` doručenek ze 21. 10. (#383–#425); žádný jiný rozdíl |

Interaktivní relace drží svůj MCP proces ze startu; nový `doctor` je k dispozici hned přes CLI.

**Rollback:** zastavit služby, `current` atomicky zpět na `runtime-0175752`, spustit, ověřit.
