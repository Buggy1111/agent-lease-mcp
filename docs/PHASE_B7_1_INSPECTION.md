# B7.1 — model a čtecí detekce kontrol

Stav: místní implementace k review. **Nenasazeno, bez nezávislého schválení.** Navazuje na změřený B7.0 pilot a Michalův pokyn pokračovat v sólo režimu. Žádné zprávy, accept ani verdict oznámení Adě.

## Použitelné chování

Nový příkaz kandidátní verze:

```sh
agent-lease quality inspect --repo /path/to/repository --commit HEAD
agent-lease quality inspect --repo /path/to/repository --commit FULL_SHA --base BASE_SHA
```

Vrací JSON **plánu**, nikoli výsledek testů. `kind="inspection"`, `executed=false`, `execution_allowed=false`; žádný projektový příkaz, build backend, npm skript, instalátor nebo hook se nespouští. Příkaz nepotřebuje AGENT_NAME ani běžící broker, místnost neotevírá a nic nezapisuje do DB/souborů. Návrat 0 znamená vytvořený plán, nikoli Quality Gate PASS; SETUP REQUIRED či neplatný repo/commit/base mají návrat 2.

Tento příkaz **není v nasazeném runtime 9134485**. Pro místní ověření z pracovního checkoutu se použije jeho `src` přes PYTHONPATH a již existující ověřený Python; globální instalace, hooky a konfigurace agentů se nemění.

## Neměnný vstup a podporované projekty

- Git root a plný commit se řeší existujícím Git hardeningem z `workspace.py`. Další metadata se čtou přes Git object ID, ne z měněných souborů checkoutu. Rozpracované změny jsou jen příznak `working_tree_dirty`; nemění obsah/digest plánu. Root je kanonický lokální identifikátor repa, takže dvě stejnojmenné složky nesplývají.
- Python: platný commitnutý `pyproject.toml`; deklarované pytest/Ruff/mypy/pyright/coverage se evidují jako metadata. Ze samotné přítomnosti nástroje se **neodhaduje příkaz**. Ten musí být explicitně v projektovém quality configu; parser CI příkazů v této etapě není implementovaný.
- JS/TS: platný `package.json`. TypeScript rozpozná root `tsconfig.json` nebo deklarovanou závislost typescript. Použijí se přesná pojmenování skriptů `test`, `lint`, `typecheck`, `build`, `coverage` a manager npm/pnpm/yarn dle lockfilu či explicitního `packageManager`. Smí se připravit `manager run script`, nikoli skript spustit. Nejasné či konfliktní nastavení → SETUP REQUIRED.
- Současný Python a Node projekt vyžaduje explicitní výběr jazyka; tato etapa netvrdí plné pokrytí monorepa. Další enginy nemají odhadovaný adaptér, ale SETUP REQUIRED.
- Symlinky/gitlinky místo manifestů, symlink na rodiči quality configu, neplatná nebo duplicitní JSON data, hluboce zanořená či nadlimitní metadata jsou odmítnuta. Limit čteného manifestu je 256 KiB. Cíle symlinků se nečtou.

## Explicitní projektový config

Commitnutý `.agent-lease/quality.toml`, verze 1:

```toml
version = 1
language = "python"

[checks.tests]
command = ["python", "-m", "pytest", "-q"]

[checks.lint]
command = ["python", "-m", "ruff", "check", "src", "tests"]

[checks.build]
command = ["python", "-m", "build"]
```

Agent Lease dostává přesně tento config odpovídající existujícím CI příkazům. Samotná deklarace není ověření prostředí nebo povolení k běhu. Argumenty a obsahy skriptů mohou obsahovat soukromá data, proto se do veřejného JSON plánu nekopírují; vrací se jen příznak deklarace a odkaz na zdroj, např. `package.json:scripts.test`.

Config může deklarovat příkazy tests/lint/typecheck/build/coverage. Nemůže přepsat security/CRAP, `required`, prahy nebo verdict. Přímé download příkazy jako npx/npm exec/pnpm dlx se odmítají; libovolný jiný deklarovaný projektový kód stále zůstává nedůvěryhodný a nevykonatelný touto inspekcí. `enabled=false` je explicitní opt-out a vrací NOT RUN, nikdy PASS; také tato změna vyžaduje policy review před budoucím vynucením.

## Význam výsledků a vazba na review

Jednotné stavy: PASS, REVIEW, FAIL, NA, SETUP_REQUIRED, NOT_RUN. Veřejné názvy kontrol odpovídají stávajícímu UI: tests, lint, typecheck, build, coverage, security, crap. Branch/agent/timestamp jsou u inspekce neznámé a vrací se null; nevzniká předstíraná historie či log provedených kontrol.

Čistá agregační funkce je připravená pro budoucí skutečné výsledky, **není schvalovacím API**. PASS vyžaduje kompletní sadu kontrol, splněné povinné checks a schválené nezávislé review z budoucího autoritativního zdroje. Projekt nemůže uvolnit základní povinné tests/lint/build/security. Chybějící povinná kontrola/N/A není PASS; optional chybějící coverage/CRAP zůstává N/A, optional selhání vede k REVIEW. Skutečné selhání povinné kontroly má přednost před REVIEW. Duplicitní/neplatná data jsou odmítnuta.

Diff se váže na vybraný plný SHA a skutečný ancestor base. Bez explicitního base se bere první rodič, u root commitu celý strom. Změny testů (včetně smazání), CI, quality konfigurace, běžných pravidel security/lintu/coverage či package manifestů jsou označeny `policy_review_required=true`. Jde o konzervativní pravidlo podle cest, nikoli důkaz, že sémantické oslabení testu v jiném souboru neexistuje. Nezávislé review se neodvozuje z barvy ani projektu, zůstává pending.

## Ověření

- 65 cílených testů: skutečná syntetická Git repa, přesný starší commit a dirty checkout, volby manageru, neplatné/ambivalentní manifesty, symlinky včetně rodiče, limity a hluboké JSON, policy/base/deleted tests, ochrana proti Git filtrům/fsmonitor/textconv, žádné spuštění deklarovaných příkazů, žádný únik jejich argumentů a žádný kontakt s místností přes CLI.
- Celá regrese na cílovém Pythonu 3.13.18: **657 passed, 1 xfailed**, 225,45 s; jediný stávající Authlib deprecation warning. Počáteční kompletní běh byl přerušen kvůli doplnění skutečných ochranných případů a není vydáván za PASS.
- Ruff `src tests` PASS. Žádné nové runtime dependency ani změny služeb. Autor Nox, peer review pending.

## Co navazuje

Místní [B7.2a — evidence běhů](PHASE_B7_2_RUN_EVIDENCE.md) již přidává privátní
runner sink a čtecí Store/broker/snapshot/SSE/UI adaptér. Zatím jej plní pouze
syntetické testy; skutečný izolovaný runner a zbývající akceptace níže nejsou hotové.

[REA mapa dashboardu](research/2026-11-07-b7-rea-dashboard-reuse.md) dokládá existující snapshot/SSE a QG renderery, které místní B7.2a znovu používá. B7.2 stále musí doplnit skutečný izolovaný runner a ověření nástrojů/prostředí i vazbu úkol+commit. Plán inspection se **nesmí** uložit/vykreslit jako úspěšně provedený run. Následují Python/TS skutečné E2E a samostatně schválené globální připojení klientů. `quality check`, skutečné běhy projektových kontrol a hooky ještě nejsou hotové; do C se nepřechází.
