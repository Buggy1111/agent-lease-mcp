# Přenositelné regrese k Nox review #2, #4 a #5

Větev `nox/review-regressions-20261106` navazuje na přesný kandidát `7aeb90f90e5bcb6c6c301ad3ddac3c3aeb9a35f8`. Mění pouze testovací a dokumentační soubory; backendová implementace zůstává beze změn. Žádný push, merge ani deploy.

## Výsledek na původním kandidátu

**9 nových testovacích případů selhává očekávaným způsobem, 40 dosavadních kontrolních případů prochází.** Celkem 49 případů, 25,95 s, bez setup chyb a bez skipů. [JUnit výstup](results.xml).

Jde o záměrně červenou regresní větev pro přípravu opravy. Selhání nejsou označená xfail ani obcházená. Tato větev se nemá sloučit do běžné zelené větve před opravou implementace. Dokazuje chyby, neopravuje je.

Nový soubor `tests/test_nox_review_regressions.py` obsahuje:

- B6: skutečné end-to-end review root commitu, žádost o změny, benigní child a recenzentovo schválení. Přítomný syntetický marker musí blokovat schválení i v dalším kole.
- B6: textový soubor se syntetickým markerem označený přes `.gitattributes` jako `-diff`; scan nesmí tento obsah považovat za čistý. Podporovaná oprava může obsah skenovat nebo explicitně odmítnout jako nepodporovaný. Obyčejný kontrolní soubor ve stejném diffu se musí najít.
- B6: šest oddělovačů uvnitř jediného Git LF řádku — vertical tab, form feed, carriage return, next-line, Unicode line separator a paragraph separator. Každý případ má pozitivní kontrolu obyčejného souboru. Na původním parseru se všechny oddělovače chybně považují za konec řádku.
- B5: tichý proces 1,8 s s průběžným externím postupem a idle rozpočtem 0,8 s. Bez monkeypatch produkčního intervalu vzorkování musí doběhnout, nebo musí implementace nepodporovanou krátkou hodnotu explicitně validovat. Dosavadní tiché ukončení před prvním voláním callbacku nevyhoví.

Test B5 je záměrně mimo `test_progress.py`, jehož autouse fixture nastavuje `PROGRESS_SAMPLE_SECONDS=0.2` a původní problém skrývá. Všechny repozitáře/databáze/procesy jsou testovací, dočasné a izolované. Nepoužívají se skutečné přihlašovací údaje, produkční DB, tokeny, broker ani vnější API.

## Benchmark B1

`scripts/benchmark_nox_b1_reservations.py` vytváří samostatnou dočasnou databázi. Data vkládá jednou transakcí před měřením, včetně aktivních lease tokenů vytvořených pouze pro test, attempts=1 a budoucí expirace. Aktivní worksety se nepřekrývají, všechny pending úkoly blokuje první rezervace. Měří pouze `lease_next`.

[Strojově čitelné výsledky a verze Python/SQLite](benchmark.json).

| Aktivní / blokované pending | Doba v posledním instrumentovaném běhu | Načtení rezervací |
| --- | --- | --- |
| 200 / 200 | 0,2238 s | 200 |
| 1000 / 1000 | 7,2710 s | 1000 |

Počet dotazů přímo dokládá opakované načítání. Instrumentace SQLite trace callbackem je součástí uvedeného wall time, proto tyto časy nelze bez výhrady porovnávat s během bez trace. Předchozí běh se stejným konzistentním seedem bez instrumentace měl 0,1743 s a 4,6991 s; zátěž stroje se mezi běhy mění. Počet dotazů rozpoznává SQL tvar tohoto kandidáta a není novým API kontraktem ani pytest asercí na budoucí implementaci.

Benchmark ověřuje, že se žádný blokovaný úkol nepřidělí a pending fronta se nezmění. Doby závisí na stroji a zatížení; nedělá se z nich nespolehlivý pytest limit ani univerzální SLA. Pro srovnání uvádí skutečný SQLite `busy_timeout`. První běh této relace před doplněním tokenů do syntetického seedu naměřil 0,277 s (200/200) a 5,978 s (1000/1000); autoritativní přiložený JSON je z opakování s konzistentními aktivními lease řádky.

## Opakování

Z kořene tohoto checkoutu s projektovým Pythonem:

```bash
PYTHONPATH=src python -m pytest -q tests/test_nox_review_regressions.py \
  tests/test_secret_scan.py tests/test_progress.py tests/test_task_paths.py \
  tests/test_routing.py --junitxml=/tmp/nox-regressions.xml
PYTHONPATH=src python scripts/benchmark_nox_b1_reservations.py \
  --counts 200 1000 --output /tmp/nox-b1-benchmark.json
ruff check tests/test_nox_review_regressions.py scripts/benchmark_nox_b1_reservations.py
```

Po opravě Adou se stejné nové testy mají stát zelenými. Je třeba zároveň spustit příslušné stávající regrese a potřebnou plnou sadu. Původní nezměněný kandidát měl v předchozím review plnou sadu 574 passed / 1 xfailed; tato testovací větev záměrně přidává další dosud selhávající scénáře.

## Předání

Ada má oficiální `changes_requested` ve review #2/#4/#5 a podrobnosti v [původním reportu](../../NOX_REVIEW_REPORT_2026-11-06.md). Tyto testy jsou doplněk k opravám, nikoli převzetí její implementace nebo povolení sloučení/nasazení. Žádný verdikt se tím nemění.
