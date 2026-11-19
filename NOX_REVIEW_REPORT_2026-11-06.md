# Nezávislé review Adiných změn #1–5

Nox, 6. 11. 2026. Čtení přesných commitů a testování v izolovaném worktree `~/dev/work/agent-lease-nox-reviews-20261106`, HEAD `7aeb90f90e5bcb6c6c301ad3ddac3c3aeb9a35f8`. Zdrojový kandidát nebyl měněn. Žádný push, merge, deploy, změna služeb, konfigurace nebo práce s reálnými přihlašovacími údaji. Reprodukce skenu použily pouze syntetický marker vytvořený z testovacích fragmentů a do reportu neukládají jeho hodnotu.

## Verdikty odeslané nástrojem verdict

| Review | Commit | Výsledek |
| --- | --- | --- |
| #1 A11, dokumentační živý test | 7f4a64a4a35fc84e330fa19d67f8c27769748550 | APPROVED — pouze dokumentační diff, nejde o nové ověření produkčního nasazení/WSL |
| #2 B1, task_paths | 31f6afc8787f3fdfd76e2a8f5db435558bf59583 | CHANGES REQUESTED — P2 výkon pod zápisovým zámkem |
| #3 B2, směrování | 1b625bb25413fd32c12f5e0936d74ef482909895 | APPROVED — žádný nález v kontrolovaném diffu |
| #4 B5, nečinnost | eefc55cd76f03f732e1d9cb5d97ae9344be929d5 | CHANGES REQUESTED — P2 krátký idle limit předběhne první vzorek postupu |
| #5 B6, bezpečnostní scan | 7aeb90f90e5bcb6c6c301ad3ddac3c3aeb9a35f8 | CHANGES REQUESTED — tři reprodukované mezery skenu, security fail |

B1 a B2 zdrojové soubory a jejich přidané testy jsou v integračním checkoutu bajtově shodné s příslušnými review commity (`git diff --quiet` ověřeno). B5/B6 nálezy byly ověřeny na přesném B6 checkoutu; změna B6 příslušné místo B5 nemění.

## Nálezy

### B6 P1 — root commit ztratí rozsah kontroly po novém kole

`workspace.py:154–157`, `server.py` request/submit a verdict, `_reviews.py` base scope.

Root commit obsahuje syntetický marker a autor bez explicitního base založí review. `parent_commit(root)` vrátí prázdný řetězec, první scan správně najde 1 marker. Po `changes_requested` autor přidá benigní child bez odstranění markeru. Uložené base zůstává prázdné; `commit_diff(child, '')` skenuje jen child. Submit i recenzentův scan vrátí `[]` a skutečné `server.verdict(... approved, security=pass)` uspěje, ačkoli marker zůstává v souboru z root commitu.

Ověřeno: `initial_findings=1`, `base_empty=true`, `child_findings=0`, `approved_with_marker_still_present=true`.

Oprava musí uchovat immutable root scope (například explicitní empty-tree základ či root-specifický režim) pro všechna další kola; přidat end-to-end regresi až do odmítnutí schválení.

### B6 P1 — git atribut může skrýt textový marker jako binary diff

`workspace.py:138–151`.

Nad benigním basem přidat `.gitattributes` s `config.txt -diff` a textový `config.txt` s běžným syntetickým markerem podporovaným vzorem. `commit_diff` vrátí `Binary files ... differ`, žádný přidaný obsah. `scan_diff=[]`, přestože přímý regex marker rozpoznává. Nejde o neobvyklý formát tajemství; je to standardní text skrytý diff atributem.

Ověřeno: `binary_message_in_diff=true`, `findings=[]`, `marker_matches_direct_pattern=true`.

Řešit textový scan blobu/diffu bez možnosti vynutit binary atribut, nebo jasně fail-closed označit neskenovatelný obsah. Binary marker se nesmí rovnat čistému scanu. Přidat regresi.

### B6 P1 — splitlines neodpovídá Git LF řádkům

`secret_scan.py:40`.

Jediný přidaný Git řádek obsahuje `prefix`, vertical tab (U+000B), syntetický marker a LF. Python `str.splitlines()` z něj vytvoří dva parserové řádky. První spotřebuje počet nových řádků hunka; druhý s markerem se přeskočí jako metadata mimo hunk.

Ověřeno: marker je přímo v diffu, diff není binary, `findings=[]`. Parser má dělit po LF a uchovat řídicí znaky uvnitř Git řádku. Přidat regresi VT a dalších oddělovačů, které Python považuje za řádky, ale Git ne.

### B5 P2 — krátký limit nečinnosti zruší i prokazatelně aktivní příkaz

`bridge.py:293–305`.

Bez testového monkeypatch má `PROGRESS_SAMPLE_SECONDS=30`. Tichý příkaz spí 1,8 s, absolutní timeout 5 s, idle_timeout 0,8 s a callback `progress` vždy vrací True. Běh skončí `stalled` ještě před prvním vyhodnocením callbacku, který má 0 volání. Veřejné rozhraní tyto kladné hodnoty neodmítá. Stávající testy problém skrývají globálním fast_sampling=0,2.

Přizpůsobit interval idle limitu a případně CPU práh délce okna, nebo příliš krátké parametry explicitně validovat. Otestovat se skutečným produkčním intervalem. Výchozí 30min konfigurace tímto konkrétním případem netrpí.

### B1 P2 — opakované načítání rezervací pro každý blokovaný kandidát

`_claims.py:117–152`, `_deliveries.py` kandidátní smyčka v `lease_next`.

Každý pending kandidát znovu načítá všechny aktivní task_paths a vytváří Claim objekty, vše pod `BEGIN IMMEDIATE`. Izolovaný benchmark má aktivní neprotínající se rezervace, každá pending práce je blokovaná první z nich; měřený je pouze `lease_next`, seed proběhl předem v jedné transakci.

| Aktivní / blokované pending | Doba lease_next | Výsledek |
| --- | --- | --- |
| 200 / 200 | 0,209 s | None |
| 1000 / 1000 | 7,234 s | None |

Čas je měření na tomto stroji, nikoli univerzální SLA. Pro srovnání `Store._connect` nastavuje SQLite `busy_timeout=5000`; jeden takový průchod tedy může držet writer lock déle než timeout jiného writeru. Funkční kontrola souběhu vypadá správně; požadovaná oprava je načíst snapshot rezervací jednou v téže transakci / použít early-exit pro dotaz existence konfliktu a přidat regresi počtu načítání. Zachovat task_id i chování stejného adresáta.

## Ověření

- Plná integrační regrese přesného 7aeb90f: **574 passed, 1 xfailed**, 208,74 s.
- Ruff kontrolovaných změněných modulů: **PASS**.
- `git diff --check` B1, B2, B5 a B6 rozsahů: **PASS**.
- Reprodukce výše byly provedeny nezávisle mimo existující testy, které prošly.
- Testy a reprodukce nepoužily produkční DB, skutečné klíče, vnější API ani živý emergency stop.

## Další krok

Ada opraví #2/#4/#5 a předloží nové přesné commity. Nox poté provede re-review, bez automatického schválení změněného commitu. Jeho vlastní premium UI 63b4f30 nadále čeká na Adino review #6. Samotné schválení #1/#3 není souhlas s push, merge nebo deploy.
