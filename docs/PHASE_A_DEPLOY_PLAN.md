# Fáze A: plán provozního nasazení (příprava 3. 11. 2026)

**Stav: NEPROVEDENO.** Čeká na: výsledek 8h chaosu v novém runtime, review Noxe
(kolem 7. 11.) a Michalův výslovný souhlas. Plán popisuje konkrétní kroky;
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

0. **Příprava bez výpadku:** sestavit `runtime-<commit>` vedle stávajícího stavu.
   V něm pustit `env -i …/python -I` kontrolu verze, celou sadu testů a
   `scripts/verify_sqlite_runtime.py`. Stávající služby se nemění.
1. **Okno údržby:** přes `agent-lease jobs`/web ověřit, že nic neběží (žádné
   `leased`/`started`). Zapnout `emergency_stop`, aby nic nového nezačalo.
2. **Zastavit všechny klienty DB:**
   `systemctl --user stop agent-lease-bridge@claude-code agent-lease-bridge@codex agent-lease-codex-wake agent-lease-broker`,
   ukončit web UI a **zavřít interaktivní relace Claude Code a Codexu** (jejich MCP
   procesy drží DB). Ověřit `fuser`/`lsof` nad `room.db`, že ji nikdo nemá otevřenou.
3. **Záloha:** `agent-lease backup` (integrity `ok`) a kopie celého adresáře DB
   mimo něj. Zapsat hash zálohy.
4. **Přepnutí služeb:** drop-iny `~/.config/systemd/user/<unit>.d/10-runtime.conf`
   s `ExecStart=` (vyprázdnit) a novým `ExecStart=%h/.local/share/agent-lease/current/venv/bin/…`.
   Pro bridge navíc `Delegate=yes` a `--cgroup`. Původní unit soubory zůstanou beze změny.
   Potom `systemctl --user daemon-reload`.
5. **Přepnutí MCP klientů:** `claude mcp remove agent-lease -s user` a
   `claude mcp add -s user agent-lease -- …/current/venv/bin/agent-lease-mcp`.
   Zachovat stávající env. V `~/.codex/config.toml` změnit jen `command`.
   Předtím zálohovat `~/.claude.json` a `~/.codex/config.toml`.
6. **Start:** broker, pak bridge a wake. `agent-lease doctor --json` musí hlásit
   SQLite 3.53.1 a dostupné služby. `journalctl --user -u 'agent-lease-*'` bez chyb.
   `systemctl --user show agent-lease-bridge@codex -p Delegate` = yes.
7. **Kontrola:** `PRAGMA integrity_check` = ok, fronta beze změny proti stavu
   z kroku 1. Zrušit `emergency_stop` (jen člověk, web).
8. **Živý test, jen se zvláštním souhlasem:** jeden syntetický úkol na `claude-code`
   bridge, ověřit jediné dokončení, `wait`, karanténu a stop/resume.
9. Teprve potom A11 (Windows/WSL restart) podle checklistu v `PHASE_A_PREDEPLOY_REVIEW.md`.

## Návrat zpět

1. `emergency_stop`, zastavit služby a zavřít relace agentů (jako v kroku 2).
2. Smazat drop-iny `10-runtime.conf`, `daemon-reload`. Tím se služby vrátí na
   uv tool (Python 3.14.4).
3. Obnovit `~/.claude.json` / `~/.codex/config.toml` ze zálohy z kroku 5.
4. DB: nové sloupce starý kód ignoruje, takže obnova DB není nutná. Při pochybnosti
   o integritě obnovit zálohu z kroku 3, ale **jen** se zastavenými klienty.
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
