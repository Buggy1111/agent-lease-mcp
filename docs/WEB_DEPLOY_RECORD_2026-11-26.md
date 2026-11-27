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

## Dohnání B7 a panel „Čeká na tebe“ — 27. 11. 2026 (`7745532`, review #56 + #57, security pass)

Při úklidu review se ukázalo, že schválené opravy B7 Quality Gate F1–F9 (#12, #14, 11. 11.) a #17 se
nikdy nedostaly do `main` ani do provozu. Dohnáno v #56 (Nox: shodné s 0455529/a854924), web doplněn o
panel „Čeká na tebe“ a ukazatel živých dat (#57). Full 1047 PASS vč. UI testu v Chromiu.

| Krok | Stav |
| --- | --- |
| Runtime | `runtime-7745532` (wheel SHA-256 `ee9bed7458e4f8c1ae19751fc6b9d70bcd315e9b2430443158e04cf514157508`), `current` přepnut |
| Web | restartován, živě ověřeno: přihlášení kódem, cookie bez klíče 403, panel Čeká na tebe, ukazatel „Živě“, 0 chyb |
| Broker, mosty, wake | **nerestartovány**, běží dál na `runtime-97aebc7` (stará verze B7) |

### Čeká na Michala: aktivace oprav B7 (podle PHASE_B7_ACTIVATION_PLAN.md vyžaduje souhlas vlastníka)

Nový kód B7 se v brokeru projeví až po jeho restartu a potřebuje dvě nastavení, jinak kontroly kvality
bezpečně skončí jako SETUP REQUIRED:

1. `~/.agent-lease/quality-runner.toml` — přidat schválený základ (plný SHA commitu, který člověk prohlásí
   za zkontrolovaný; navrženo `7745532` z main po review #56/#57):
   `[policy_bases]` → `"/home/buggy1111/dev/projects/agent-lease-mcp" = "<plný SHA>"` (cesta podle `repositories`).
2. Drop-in brokeru `20-quality-limits.conf` doplnit o `Delegate=cpu memory pids` a `DelegateSubgroup=broker`.
3. Pak restart všech služeb v okně bez běžících úkolů a ověření (integrita DB, fronta, `doctor`, jeden běh kontroly).

Do té doby: Quality Gate běží ve staré verzi bez oprav F1–F9 (jako dosud od 20. 11.).

**Zjištěno navíc:** všechny tři operátorské politiky (`quality-runner.toml`, `fix-runner.toml`, `research.toml`)
mají v `repositories` starou cestu `/home/buggy1111/dev/projects/agent-lease-mcp`, která po přestěhování repa
do `~/dev/mcp-servers/agent-lease-mcp` neexistuje. Kontrola kvality, automatické opravy i výzkum nad repem
Agent Lease proto teď skončí odmítnutím (bezpečně, ale nefungují). Oprava = nahradit cestu ve všech třech
souborech (spolu s krokem 1 výše); politiky mění jen vlastník.

## Aktivace oprav B7 — 27. 11. 2026 ~12:20 (Michal: „aktivuj“)

| Krok | Výsledek |
| --- | --- |
| CI | červená opravena (review #58): ruff v testech; sandbox E2E poprvé v GitHub CI pod uživatelem runneru → 36/36 PASS |
| Politiky | `quality-runner.toml`, `fix-runner.toml`, `research.toml`: cesta repa `~/dev/mcp-servers/agent-lease-mcp` (0600 zachováno, záloha `deploy-backup-*-ada-b7activate`) |
| Základ | `[policy_bases]` = `d612eb084ad45dc208b3f05b77c63cb7e45c576a` (main po #56/#57/#58) |
| Broker | drop-in: `Delegate=cpu memory pids`, `DelegateSubgroup=broker`, `ExecStartPost` zapne kontroléry v rodičovské cgroup (jinak „controllers are unavailable“) |
| Restart | okno bez leased/started; DB integrita ok, fronta shodná; 5 služeb active na `runtime-7745532` |
| Ověření | skutečná kontrola `d612eb0`: tests/lint/build PASS v sandboxu, security REVIEW (čeká na review navázané na commit) — správně; starší commit než základ odmítnut (F2) |
