# Fáze A: plán provozního nasazení (příprava 3. 11. 2026)

**Stav: NEPROVEDENO.** Čeká na: 8h chaos přesného kandidáta (po opravě Nox #566/P1),
review oprav Noxem a Michalův výslovný souhlas. Plán doplněn 4. 11. podle review #566 (P2). Plán popisuje konkrétní kroky;
spouštět je jen po schválení a nejlépe společně s Michalem.

## Současný provoz (inventura 3. 11. 2026, jen čtení)

| Součást | Odkud běží | Python / SQLite | Kód |
|---|---|---|---|
| `agent-lease-broker.service` | `~/.local/bin/agent-lease-broker` → uv tool `~/.local/share/uv/tools/agent-lease-mcp` | 3.14.4 / **3.50.4** | starší snímek repa, bez `cgroup.py`, `run_fence.py`, `launch_gate.py`, `process_guard.py` |
| `agent-lease-bridge@claude-code`, `@codex` | totéž, `--agent %i`, bez `--cgroup` | 3.14.4 / 3.50.4 | totéž |
| `agent-lease-codex-wake.service` | totéž | 3.14.4 / 3.50.4 | totéž |
| MCP server v Claude Code (user scope) | `~/dev/projects/agent-lease-mcp/.venv/bin/agent-lease-mcp` | vývojové `.venv` / 3.50.4 | **aktuální pracovní kopie**, editable |
| MCP server v Codexu (`~/.codex/config.toml`) | totéž `.venv` | totéž | totéž |

Důsledky:

- Služby a MCP klienti dnes běží na **různých verzích kódu**. Nové commity se do MCP
  projeví hned při dalším startu agenta, do služeb až po reinstalaci. Schéma DB je
  zatím zpětně kompatibilní (nové sloupce jen přibývají a starý kód je ignoruje).
  Nasazení by tento rozjezd mělo ukončit.
- MCP klient spouštěný z vývojového repa znamená, že rozpracovaná změna v `src/`
  se dostane do živého agenta bez review. Cílový stav: všichni klienti běží
  z jednoho připnutého runtime, ne z pracovní kopie.

## Cílový stav

- Jeden runtime `~/.local/share/agent-lease/runtime-<commit>/`:
  - `python/`: standalone CPython 3.13.18+20261026 se zabudovanou SQLite 3.53.1,
    SHA-256 archivu ověřený proti upstream `SHA256SUMS`, viz
    `PHASE_A_A8_DISTRIBUTION_PROPOSAL.md`,
  - `venv/`: závislosti přesně podle `uv.lock` a wheel z otestovaného commitu.
- Symlink `~/.local/share/agent-lease/current` → `runtime-<commit>` (návrat = přepnutí symlinku).
- Všechny služby i oba MCP klienti spouští `current/venv/bin/agent-lease-*`.
- Bridge s `--cgroup` a `Delegate=yes` (drop-in), `KillMode=control-group` (výchozí) ponechat.

## Postup (každý krok až po souhlasu)

0. **Příprava bez výpadku:** sestavit runtime **přímo ve finální cestě**
   `~/.local/share/agent-lease/runtime-<commit>/` (venv není přemístitelný).
   Archiv stáhnout z přesné URL release `20261026`, ověřit proti uloženému
   manifestu `docs/test-results/phase-a-a8-pbs/SHA256SUMS` a manifest i URL
   zapsat do `runtime-<commit>/IDENTITY`. V runtime pustit `env -i …/python -I`
   kontrolu verze, celou sadu testů a `scripts/verify_sqlite_runtime.py`.
   Zkontrolovat, že shebangy `venv/bin/agent-lease*` ukazují do `runtime-<commit>`.
   Stávající služby se nemění.
1. **Okno údržby:** přes web ověřit, že nic neběží (žádné `leased`/`started`).
   Zapnout `emergency_stop`, aby nic nového nezačalo. (`jobs` je omezený výpis pro
   adresáta, globální snímek fronty se dělá až v kroku 3.)
2. **Zastavit všechny klienty DB:**
   `systemctl --user stop agent-lease-bridge@claude-code agent-lease-bridge@codex agent-lease-codex-wake agent-lease-broker`,
   ukončit web UI a **zavřít interaktivní relace Claude Code a Codexu** (jejich MCP
   procesy drží DB). Ověřit `fuser`/`lsof` nad `room.db`, že ji nikdo nemá otevřenou.
3. **Offline záloha a snímek fronty** (broker stojí, proto bez broker-only env a
   přes absolutní cestu nového interpretu, ne CLI z `PATH`):
   ```
   R=~/.local/share/agent-lease/runtime-<commit>
   env -u AGENT_LEASE_REQUIRE_BROKER -u AGENT_LEASE_SOCKET AGENT_LEASE_DB=<cesta k room.db> \
     $R/venv/bin/agent-lease backup
   ```
   Ověřit `PRAGMA integrity_check` = ok u **zdrojové DB i zálohy** (přímo přes
   `$R/python/bin/python3 -I` a `sqlite3` v režimu `mode=ro`), zapsat SHA-256 zálohy.
   Celý adresář DB zkopírovat mimo něj. Globální snímek fronty uložit do souboru:
   `SELECT message_id, recipient, state, attempts, last_error FROM deliveries
   WHERE state IN ('pending','leased','started','needs_review')` (read-only spojení).
4. **Přepnutí runtime a služeb:**
   - Uložit původní stav: `readlink ~/.local/share/agent-lease/current` (při prvním
     nasazení neexistuje, zapsat „nebyl“) a archiv všech stávajících adresářů
     `~/.config/systemd/user/agent-lease-*.d/` (`tar czf drop-ins-<datum>.tgz`).
   - Atomicky založit/přepnout symlink:
     `ln -sfn runtime-<commit> ~/.local/share/agent-lease/current.new &&
      mv -T ~/.local/share/agent-lease/current.new ~/.local/share/agent-lease/current`,
     ověřit `readlink` a existenci `current/venv/bin/agent-lease-{broker,bridge,mcp}`.
   - Drop-iny `~/.config/systemd/user/<unit>.d/10-runtime.conf`
   s `ExecStart=` (vyprázdnit) a novým `ExecStart=%h/.local/share/agent-lease/current/venv/bin/…`.
   Pro bridge navíc `Delegate=yes` a `--cgroup`. Původní unit soubory zůstanou beze změny.
   Potom `systemctl --user daemon-reload`.
5. **Přepnutí MCP klientů:** `claude mcp remove agent-lease -s user` a
   `claude mcp add -s user agent-lease -- …/current/venv/bin/agent-lease-mcp`.
   Zachovat stávající env. V `~/.codex/config.toml` změnit jen `command`.
   Předtím zálohovat `~/.claude.json` a `~/.codex/config.toml`.
6. **Start a ověření skutečných procesů:** broker, pak bridge a wake.
   `doctor` hlásí jen SQLite **svého** procesu, nestačí. Pro každou službu:
   `systemctl --user show <unit> -p ExecStart -p MainPID -p Delegate`, pak
   `readlink /proc/<MainPID>/exe` (musí být `runtime-<commit>/python/bin/python3.13`)
   a `/proc/<MainPID>/cmdline` (musí být `current/venv/bin/…`). Tentýž interpret je
   v kroku 0 ověřený na SQLite 3.53.1. Totéž pro **oba čerstvě spuštěné MCP
   klienty** (`pgrep -af agent-lease-mcp` po startu nové relace Claude Code i Codexu).
   Doctor spouštět absolutně: `current/venv/bin/agent-lease doctor --json`.
   U obou bridge ověřit `Delegate=yes` **a** skutečné umístění v cgroup
   (`cat /proc/<MainPID>/cgroup`, `systemd-cgls --user-unit agent-lease-bridge@codex`).
   `journalctl --user -u 'agent-lease-*'` bez chyb.
7. **Kontrola:** `PRAGMA integrity_check` = ok, fronta beze změny proti stavu
   z kroku 1. Zrušit `emergency_stop` (jen člověk, web).
8. **Živý test, jen se zvláštním souhlasem:** jeden syntetický úkol na `claude-code`
   bridge, ověřit jediné dokončení, `wait`, karanténu a stop/resume.
9. Teprve potom A11 (Windows/WSL restart) podle checklistu v `PHASE_A_PREDEPLOY_REVIEW.md`.

## Návrat zpět

1. `emergency_stop`, zastavit služby a zavřít relace agentů (jako v kroku 2).
2. Obnovit drop-iny přesně z archivu z kroku 4 (smazat nové `10-runtime.conf`,
   rozbalit původní stav), symlink `current` vrátit na zapsaný původní cíl
   (nebo odstranit, pokud dřív nebyl), `daemon-reload`. Tím se služby vrátí na
   uv tool (Python 3.14.4).
3. Obnovit `~/.claude.json` / `~/.codex/config.toml` ze zálohy z kroku 5.
4. DB: nové sloupce starý kód ignoruje, takže obnova DB není nutná. Při pochybnosti
   o integritě obnovit zálohu z kroku 3, ale **jen** se zastavenými klienty; vadnou
   DB zachovat stranou a `emergency_stop` nechat zapnutý, dokud člověk neověří
   účinky úkolů provedených po záloze (viz `PHASE_A_PREDEPLOY_REVIEW.md`).
5. Spustit služby, `doctor --json`, integrity check. Po restartu se `needs_review`
   ani `pending` úkoly nesmí spustit samy bez kontroly.

## Rizika a otevřené body

- Za běhu nesmí zůstat žádný klient se starým kódem: běžící procesy se změnou
  souborů nepřepnou. Proto se zavírají i interaktivní relace.
- `--cgroup` je v provozu poprvé. Pokud bridge hlásí nedostupnou delegaci, nechat
  ho zastavený (fail-closed) a vrátit se k návratu, místo vypínání pojistky.
- Upgrade uv není součástí plánu. Standalone Python se instaluje ručně z
  ověřeného archivu.
- Interaktivní MCP po přepnutí přestane sledovat pracovní kopii. Vývojové změny
  se pak do agentů dostanou jen novým runtime, což je záměr.
