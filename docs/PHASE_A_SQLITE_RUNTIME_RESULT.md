# A8: izolovaný kompletní runtime s opravenou SQLite

Datum: 1. 11. 2026. Zdrojový commit: `8fbb132301a7e93fc807609e640bd091a9979bb1`.
**MCP, broker, bridge a syntetický worker prošly se SQLite 3.54.1.**
Jde o lokální experiment s kompletním instalovaným balíčkem. Provozní služby,
klienti ani jejich databáze nebyli aktualizováni; A8 není provozně uzavřeno.

## Prostředí a způsob načtení

- Adresář: `/tmp/agent-lease-a8-runtime`; vlastní `venv`, zdrojová kopie,
  wheel a nové syntetické DB. Starý 8h snapshot i runtime zůstaly zachovány.
- Systémový CPython 3.12.3, FastMCP 3.4.10; runtime závislosti podle aktuálního
  `uv.lock`, Python 3.12 binární závislosti doplněny z PyPI. Dev verze také
  připnuty exportem z locku. [Instalované verze](test-results/phase-a-a8/installed.txt),
  [export požadavků](test-results/phase-a-a8/requirements.lock).
- Aktuální projekt sestaven přes `uv build`, wheel instalován do nového venv.
  Obsah všech 37 Python modulů v balíčku porovnán s repozitářem: shodný.
  [Identita artefaktů](test-results/phase-a-a8/artifact-identity.json).
- Použita stejná knihovna SQLite 3.54.1 z dřívějšího APSW experimentu; její
  původ a ověření RECORD jsou v [přednasazovacím auditu](PHASE_A_PREDEPLOY_REVIEW.md).
  SHA-256: `168e48ea59c72fcbe3c4d5a92dcffc4f338775e3879265f065198c617e4ac1ae`.
- Kopie knihovny je pouze v novém venv jako `lib/libsqlite3-a8.so`.
  Vlastní `.pth` importuje [bootstrap](test-results/phase-a-a8/a8_sqlite_bootstrap.py),
  který ověří hash, načte knihovnu přes `ctypes.RTLD_GLOBAL` před stdlib
  `sqlite3` a ověří Python i SQL verzi. Při neshodě ukončí proces s kódem 78.
  Nejde o náhradu stdlib API za APSW ani o globální LD_PRELOAD.

## Ověřené chování

| Kontrola | Výsledek / důkaz |
|---|---|
| Nový proces s prázdným prostředím, `python -I` | Python i SQL používají 3.54.1; [výsledek](test-results/phase-a-a8/clean-child.json) |
| Chybějící nebo poškozená knihovna v odděleném zkušebním venv | Startup odmítnut, exit 78; [výsledek](test-results/phase-a-a8/bootstrap-refusal.json) |
| Skutečné MCP stdio | Initialize, 18 nástrojů, claim → owner → release; [výsledek](test-results/phase-a-a8/runtime-check.json) |
| Samostatný broker a bridge z instalovaného wheelu | Syntetický úkol dokončen; každý proces před vlastním main potvrdil SQLite 3.54.1 |
| Podproces bridge | Vlastní neškodný Python worker potvrdil 3.54.1; žádné skutečné CLI agenta ani provider API |
| Záloha, DB integrita | Broker backup a výsledná DB `ok` |
| SIGKILL zapisujícího procesu a offline WAL → DELETE | Rollback i integrita prošly; [výsledek](test-results/phase-a-a8/store-crash-migration.json) |
| Bridge regrese | 92 passed v 46,02 s |
| Celá regresní sada po instalaci wheelu | 478 passed, 1 xfailed, 1 warning v 169,58 s; [úplný log](test-results/phase-a-a8/pytest.log) |
| Ruff (`src`, `tests`, nový ověřovací skript) | Prošlo |

Xfail je známý scénář odděleného potomka bez cgroup; tento běh jej neřeší.
Warning je existující deprecace `authlib.jose`, nikoli chyba SQLite.

Ověřovací skript [scripts/verify_sqlite_runtime.py](../scripts/verify_sqlite_runtime.py)
vyžaduje **nový** výstupní adresář a ověřuje instalovaný balíček bez PYTHONPATH.
Vytváří pouze vlastní syntetickou místnost a spuštěný broker uklízí ve `finally`.
Po připravení kandidátního runtime lze kontrolu zopakovat mimo pracovní kopii:

```sh
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 \
  /tmp/agent-lease-a8-runtime/venv/bin/python \
  /home/buggy1111/dev/projects/agent-lease-mcp/scripts/verify_sqlite_runtime.py \
  /tmp/agent-lease-a8-runtime/check-next
```

## Reprodukce experimentu a omezení distribuce

1. V novém vlastním adresáři vytvořit venv pomocí systémového `/usr/bin/python3`
   (ověřen 3.12.3, dynamicky vázaná `_sqlite3`). Nepoužít staticky vázaný uv Python.
2. Exportovat `uv.lock` s extra `dev` a instalovat jeho přesné verze. Sestavit
   wheel z uvedeného commitu a instalovat jej před testy i podprocesovými kontrolami.
3. Po ověření původu a výše uvedeného hashe kopírovat knihovnu do tohoto venv
   jako `lib/libsqlite3-a8.so`. Bootstrap umístit do jeho `site-packages` pod
   názvem `a8_sqlite_bootstrap.py`; nový `00_a8_sqlite.pth` obsahuje jediný řádek
   `import a8_sqlite_bootstrap`. To je popis lokálního experimentu, ne pokyn
   k editaci existujícího produkčního prostředí.
4. Ověřit čistý podproces, odmítnutí vadné knihovny, celý suite a skript výše.
   Změna interpretru, knihovny nebo balíčku vyžaduje nové ověření.

Bootstrap je specifický pro tento Linux runtime a připnutou knihovnu. Python
`-S` zpracování `.pth` vypíná a touto cestou **není pokrytý**. Mechanismus není
bezpečnostní hranicí proti uživateli, který může upravit venv. Zdejší úspěch
nenahrazuje audit distribuovaného binárního artefaktu, jeho licencí a sestavení.
Do repozitáře není přidána cizí binární knihovna ani produkční bootstrap.

Dřívější plná sada spuštěná před instalací projektového wheelu byla přerušena:
watchdog podprocesy hlásily `ModuleNotFoundError: No module named 'agent_lease_mcp'`.
Byla to chyba pořadí přípravy; výsledek tohoto běhu není úspěšné ověření.
Čistá sada byla spuštěna znovu až po instalaci. Samotný import ze `src`
v hlavním pytest procesu nestačí pro nezávislé watchdog podprocesy.

## Co zbývá před provozním uzavřením A8

- Nezávisle zrevidovat vybraný způsob dodání opravené SQLite a připravit
  reprodukovatelný distribuční balíček se zdrojem/verzí knihovny a rollbackem.
- Po souhlasu Michala přepnout všechny skutečné služby **a** MCP klienty na
  stejné ověřené prostředí. Pouhá změna souborů neaktualizuje běžící procesy.
- Provozní kontrola verzí a integrity, test restartu Windows/WSL a ostatní
  otevřené body A1/A6/review. Starý úspěšný 8h běh nepředstavuje 8h ověření
  této nové kombinace Pythonu, knihovny a závislostí.
