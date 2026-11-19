# Reprodukční podklady pilotu 9134485

Tyto soubory jsou **měřicí harness pro konkrétní lokální pilot**, nikoli implementace B7 CLI, instalátor, scheduler nebo produkční bezpečnostní hranice. Nespouštějí se automaticky. Autor Nox; nezávislé review zatím pending.

Před použitím přečíst [výsledky a omezení](../2026-11-07-b7-0-pilot.md). Runtime cesty a identita uživatele odpovídají změřenému stroji. Jiný stroj vyžaduje ověření interpreteru, pinů, mountů, cgroup, explicitní souhlas a nový preflight; nikdy místo nedostupného sandboxu nepouštět repo bez izolace.

Při reprodukci vytvořit nový vlastní `<scratch>` a následující rozložení:

| Cíl | Obsah |
| --- | --- |
| `<scratch>/snapshot` | Čistý `git archive` přesného 9134485371fdd6d1c92c16562aa04d188c3f2125, bez přidaných harness souborů |
| `<scratch>/run_pilot.py`, `pilot_scope.py` | Hostitelský launcher, pouze ověřené lokální příkazy |
| `<scratch>/pilot/run-with-identity.sh` | Pilotní kopie původního obalu, přidané jen RO syntetické passwd/group |
| `<scratch>/pilot/tooldeps` | Ověřené pinnuté wheels rozbalené s původními notices; `preflight.py` uložit jako `b7_preflight.py`; dále scan/compare/CRAP skripty, scanner.toml a empty-ignore |
| `<scratch>/pilot/tooldeps/passwd`, `group` | Syntetická položka aktuálního UID/GID, HOME pouze `/tmp/home`; nikdy hostitelské soubory |
| `<scratch>/pilot/tooldeps/bin` | Dvě ověřené scanner binárky; před redakční zkouškou nikdy historie |
| `<scratch>/pilot/out/<fáze>` | Výstupy, scope důkazy, skutečný rc a čas, oddělené po bězích |
| `<scratch>/pilot/tooldeps/inputs` | JSON coverage a původní lizard CSV stejného SHA; syntetický korpus pro B6 srovnání |
| `<scratch>/pilot/history` | Samostatná lokální Git kopie bez hooků/remotes: jediný tip 9134485 a jeho předci |
| `<scratch>/artifacts/history-scope.json` | Ověřený SHA a nenulový commit_count před historickým během |

Piny a hashe jsou v `docs/test-results/b7-0-9134485/*tools.json`; původní lizard CSV a coverage JSON ve stejném adresáři. Je nutné mít i obvyklé projektové runtime/test závislosti v ověřeném R/TD. Cesty v launcheru jsou záměrně konkrétní a vyžadují před použitím review; dokumentace je nesmí vydávat za přenositelnou hotovou integraci.

Pořadí: preflight → coverage/baseline/lizard → coverage JSON → ruční CRAP kontroly a report; nezávisle syntetický scanner korpus a redakce všech výstupů → teprve platná historie. Žádné oslabování testů, vynechávání selhání, automatické nastavování PASS ani validace živých klíčů. Limit každé dávky je 2 GiB / 100 % CPU / 15 minut, uvnitř 50 MiB na soubor a 256 procesů. Předchozí chybné pokusy jsou popsány v reportu, nikoli započteny jako úspěch.
