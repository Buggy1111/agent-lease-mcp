# Nasazení webu `bf5c78c` (neonové UI, web jako služba) — 26. 11. 2026, Ada

Michal 26. 11.: „začni s tím webem … abych pořád nemusel ti říkat, že máš otevřít web od agent lease,
ať už konečně vás můžu řídit z toho webu“. Review #54 (nahrazuje #49) schválil Nox na `8881e37`, security pass.

| Krok | Výsledek |
| --- | --- |
| Merge | `main` = `bf5c78c`, pak `fa89060` (web-login bez AGENT_NAME pro ikonu, review #54 schváleno znovu) (merge `ada/ui-neon-20261121`), full 1006 PASS / 30 SKIP / 1 XFAIL |
| Runtime | `runtime-bf5c78c`: interpret zkopírován z `runtime-97aebc7`, závislosti podle `pip freeze` živého runtime — **shodné**, `pip check` OK |
| Artefakt | wheel SHA-256 `ba6e990277f475c2ba13fd3109c20ccfb16b8b34aa34c0c84ab87b031143e552`; proti živému runtime změněny jen `webui.py`, `cli.py` a přidané `assets/` |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261126-ada-bf5c78c` (DB integrita `ok`, unit soubory, `previous-current` = `runtime-97aebc7`) |
| Přepnutí | `current → runtime-bf5c78c` atomicky. Broker, mosty a wake **nerestartovány** (jejich kód je shodný); nový runtime načtou při příštím restartu |
| Web | nová jednotka `agent-lease-web.service` (`AGENT_NAME=web`, 127.0.0.1:8765), `enable --now`, active; DB dál `journal_mode=delete` |
| Ověření | Přehled, Kvalita, Nastavení na :8765 se živými daty, bez chyb konzole; 5 služeb active |
| Přihlášení | `agent-lease web-login` (ve WSL otevře prohlížeč ve Windows), ikona **Agent Lease** na ploše; přihlášení platí 7 dní |

**Rollback:** `systemctl --user disable --now agent-lease-web`, `current` atomicky zpět na `runtime-97aebc7`.

## Bezpečnostní kolo — 27. 11. 2026 (`95e008d`, review #55, security pass)

Michal: „pořádně to dodělejte s Noxem, i tu security … ať na to nemusíme šáhnout“. Nox udělal samostatný
audit (#2098, 5×P2), opravy prošly review #55 (2 kola), full 1018 PASS.

| Oblast | Stav po nasazení |
| --- | --- |
| Přihlášení | do prohlížeče jde jen jednorázový kód na 120 s (`webui.login`, 0600), hlavní token nikdy v URL, historii ani na výstupu |
| Session | cookie + druhý klíč v localStorage (oddělený podle portu); samotná cookie → 403 |
| web-login | kód jen na port, jehož posluchač patří tomuto uživateli; ve Windows jen přes `wslrelay` (fail-closed) |
| Požadavky | Host kontrolován všude (DNS rebinding), POST jen s povoleným Origin, CSP s nonce místo `unsafe-inline` |
| Odolnost | strop 64 spojení, absolutní limit 20 s na požadavek (hlídací vlákno zavře socket), SSE končí s přihlášením |
| Služba | `NoNewPrivileges`, omezené rodiny socketů, `MemoryMax=512M`, `TasksMax=128` |
| Ověření živě | E2E: přihlášení kódem, reload, živý stream, cookie bez klíče 403, opakovaný kód 403, cizí Host 403, 0 chyb konzole; 5 služeb active, DB `delete` |

Runtime `runtime-95e008d` (proti `runtime-fa89060` změněn jen `webui.py`), wheel SHA-256
`94f1cdd3443a74898f062656691089159e3fa1b6718fc2bbcec8151abe71867e`. Broker a mosty nerestartovány.
Rollback: `current` zpět na `runtime-fa89060`, `systemctl --user restart agent-lease-web`.
