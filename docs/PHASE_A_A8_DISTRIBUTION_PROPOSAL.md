# A8: návrh distribuce opravené SQLite (3. 11. 2026)

**Stav:** krok 1 a 2 provedeny izolovaně s Michalovým souhlasem (3. 11. 2026).
Provoz, služby ani MCP klienti nebyli změněni.

## Výsledek izolovaného ověření (3. 11. 2026)

| Kontrola | Výsledek |
|---|---|
| Archiv `cpython-3.13.18+20261026-…-install_only.tar.gz` | SHA-256 `126343c5…81be` = upstream `SHA256SUMS` |
| `sqlite3.sqlite_version` / `SELECT sqlite_version()` (`python -I`, čisté env) | **3.53.1 / 3.53.1**, SQLite zabudovaná v interpretu, žádná `libsqlite3.so` |
| Nový venv + wheel z `cda7678` + závislosti z `uv.lock` | instalováno |
| Celá sada testů | 487 passed, 1 xfailed, 1 warning ([log](test-results/phase-a-a8-pbs/pytest.log)) |
| `scripts/verify_sqlite_runtime.py` (skutečné MCP stdio, broker, bridge, worker) | všechny procesy 3.53.1, 18 nástrojů, záloha a integrita `ok` ([výsledek](test-results/phase-a-a8-pbs/runtime-check.json)) |
| Identita artefaktů | [runtime-identity.json](test-results/phase-a-a8-pbs/runtime-identity.json), [requirements.lock](test-results/phase-a-a8-pbs/requirements.lock) |
| 15min chaos s cgroup (systemd scope, 4 workery, kód `cda7678`) | **PROŠLO**: 310 úkolů, 114 pádů workerů, 23 pádů brokeru, 9 limitů; žádné dvojité efekty ani Done, žádní sirotci, 0 pending, integrita `ok` ([log](test-results/phase-a-a8-pbs/chaos-15min-cgroup.log)) |

8h běh v tomto runtime: pokusy 3. 11. a 4. 11. ráno přerušilo vypnutí WSL
(žádný výsledek); 3. pokus běží 4. 11. od 14:30 na `2a69f93`. Rozhodující bude
8h běh přesného kandidáta po opravě Nox #566/P1 (`240f7d2` a novější).

## Zjištění

- Spravované Pythony od uv 0.11.12 na tomto stroji (3.10.20, 3.11.15, 3.13.13,
  3.14.4) hlásí SQLite **3.50.4**, tedy pod požadavkem A8 (≥ 3.50.7 / 3.51.3).
- Upstream python-build-standalone (zdroj Pythonů pro uv) zvedl SQLite na
  **3.53.1** commitem `9150a58b` („sqlite 3.50.4 -> 3.53.1 (#1137)“, 2. 6. 2026).
  Aktuální `pythonbuild/downloads.json` uvádí `sqlite-autoconf-3530100.tar.gz`
  z sqlite.org, SHA-256 `83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f`,
  licence public domain.
- Poslední release `20261026` obsahuje mimo jiné
  `cpython-3.13.18+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz`
  a `cpython-3.14.10+20261026-…`, k nim `SHA256SUMS`. Novější uv (aktuálně 0.13.7)
  tyto sestavení stahuje sám.
- Lokálně ověřeno 3. 11. 2026 (tabulka výše): stažené sestavení hlásí 3.53.1.
  Upstream manifest je uložen v [SHA256SUMS](test-results/phase-a-a8-pbs/SHA256SUMS)
  (z `https://github.com/astral-sh/python-build-standalone/releases/download/20261026/SHA256SUMS`);
  řádek archivu: `126343c54daff4068efc1ac269518d0cc202d57857794948103dd1454e8881be`.

## Proč je to lepší než dosavadní experiment

Dřívější izolovaný runtime (viz `PHASE_A_SQLITE_RUNTIME_RESULT.md`) načítal
knihovnu vytaženou z wheelu APSW přes `.pth` bootstrap. Přes `python -S` se dal
obejít a původ knihovny byl nepřímý. Standalone Python má SQLite přímo v sobě:

- nepotřebuje `.pth`, `ctypes`, `LD_PRELOAD` ani cizí `.so` v repozitáři,
- původ je dohledatelný: sqlite.org tarball s hashem, upstream build, release `SHA256SUMS`,
- stejný mechanismus jako dnes (uv venv), jen novější interpreter,
- návrat zpět je přepnutí venv na původní interpreter.

## Navržený postup (každý krok až po souhlasu)

1. **Izolované ověření (bez zásahu do provozu):** stáhnout přes uv
   `cpython-3.13.18+20261026` do vlastního adresáře. Ověřit SHA-256 proti
   `SHA256SUMS` a `sqlite3.sqlite_version` i `SELECT sqlite_version()` == 3.53.1.
2. V novém venv z tohoto interpretru nainstalovat wheel z aktuálního commitu
   a závislosti přesně podle `uv.lock`. Spustit celou sadu,
   `scripts/verify_sqlite_runtime.py` a chaos test.
   Pro uzavření A8 nakonec 8h chaos v tomto runtime.
3. Nezávislé review (Nox), až bude mít kapacitu.
4. **Provozní přepnutí** (zvláštní souhlas): podle plánu v
   `PHASE_A_PREDEPLOY_REVIEW.md` zastavit všechny klienty DB, zálohovat a ověřit
   integritu, přepnout služby i MCP klienty na nový venv, `doctor --json` musí
   hlásit 3.53.1. Rollback: původní venv zůstává, přepnout zpět a restartovat.

## Otevřené otázky

- `scripts/verify_sqlite_runtime.py` už kontroluje `>= 3.51.3` a shodu verze ve všech
  procesech, takže ho jde použít beze změny.
- Upgrade samotného uv (0.11.12 → 0.13.x) je změna nástroje pro všechny projekty.
  Šetrnější varianta: spustit jen jednorázově `uvx --from uv==0.13.7`, případně
  stáhnout release asset a ověřit ho ručně.
