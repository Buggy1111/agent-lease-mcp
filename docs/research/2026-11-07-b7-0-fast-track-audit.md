# B7.0 – fast-track audit před Quality Gate (7. 11. 2026, Ada)

**Revize 1 (po review Noxe #7, `2026-11-07-b7-0-independent-review.md`):** opraven CI build,
doplněna skutečná izolace cizího kódu, bezpečný pilot scannerů, upřesněný CRAP a piny zdrojů.

Read-only průzkum podle `docs/AGENT_LEASE_IMPROVEMENTS_ROADMAP.md` (B7.0) a `TODO.md` (Priorita B).
Nic se neinstalovalo, žádný pilot ještě neběžel. Licence a verze ověřeny přes GitHub API
(`gh api repos/…`, 7. 11. 2026). Čeká na nezávislé ověření Noxem (licence, bezpečnost, přínos).

## 1. Co už máme (nestavět podruhé)

| Kontrola | Kde | Poznámka pro B7 |
|---|---|---|
| Testy | `pytest` (586 testů, ~3 min), CI `ci.yml`, matice Pythonů | Zdroj výsledku **Testy** |
| Lint | `ruff check src tests` v CI | Zdroj **Lint**; formát (ruff format) se nekontroluje |
| Build | `ci.yml` krok Build (`python -m build`) v testovací matici + `release.yml`; lokálně `uv build` ověřen 7. 11. | Zdroj **Build** (existuje, žádný druhý build systém) |
| Závislosti | Dependabot (pip, actions) týdně; lokálně `pip-audit` (~/.local/bin) | Zdroj části **Security**; pip-audit v CI chybí |
| Tajemství v diffu | B6 `secret_scan.py` (review diff, fail closed) | Zdroj **Security/secrets** pro review; repo-wide scan chybí |
| Review | B3/B4 stavový automat, B6 `security="pass"` | Zdroj **REVIEW** a nezávislého verdiktu; neduplikovat |
| Chaos/izolace | `tests/chaos/run_chaos.py`, cgroup, run-fence | Mimo QG (provozní spolehlivost), jen odkaz |
| Coverage | **chybí** (coverage.py ani pytest-cov nejsou v dev deps) | Nutné pro CRAP |
| Složitost | **chybí** (žádný radon/lizard/mccabe) | Nutné pro CRAP |
| Typy | **chybí** (mypy/pyright nepoužity) | N/A pro MVP, nevydávat za PASS |
| SAST | **chybí** (Semgrep ani jiný) | Kandidát, viz licence |

Další projekty pro adaptér: `earth-pulse`, `lili` (Godot – mimo MVP), `silikon-manager` (TS/JS + PocketBase),
`czech-nlp-toolkit`, `shelly-mcp` (Python). Zapojit až po stabilním jádru (TODO).

## 2. Kandidáti

| Nástroj | Verze (7. 11.) | Licence | Offline | Pro co | Rozhodnutí |
|---|---|---|---|---|---|
| **coverage.py** | 7.17.2 | Apache-2.0 | ano | pokrytí Pythonu (řádky/větve, JSON report) | **POUŽÍT** (dev závislost, ne v runtime) |
| **lizard** | 1.25.0 | MIT oprávnění v `LICENSE.txt` @ `edbb09b` (GitHub klasifikátor NOASSERTION ≠ zákaz; zachovat notice) | ano | cyklomatická složitost Python + **TS/JS** jedním nástrojem | **POUŽÍT v dev pilotu** (Nox ověřil licenci) |
| radon | (poslední push 2024-10) | MIT | ano | složitost jen Python | VYNECHAT (lizard pokrývá i TS, radon stagnuje) |
| **Gitleaks** | v8.30.1 (2026-03-21) | MIT | ano (Go binárka) | tajemství v celé historii repa | **INSPIROVAT SE / pilot** – jeho autor přešel na Betterleaks |
| **Betterleaks** | v1.10.0 (2026-11-04) | MIT | ano jen s vypnutou validací | od autora Gitleaks; **ne plně drop-in** (online `--validation`, načítání configu z cíle, jiné limity dekódování, výchozí `redact=0`) | **PILOT spolu s Gitleaks**, jen v režimu z §5 |
| **Semgrep CE** | v1.184.0 | engine LGPL-2.1; registry pravidla Semgrep Rules License 1.0 = jen interní účely, **zakázaná i jako služba** | ano s lokálními pravidly | SAST | **INSPIROVAT SE**: případně externí engine jen s **vlastními, nezávisle napsanými** pravidly; registry pravidla do Agent Lease (MIT) ani do služby nikdy |
| eslint / tsc | eslint v /usr/bin, tsc chybí | MIT / Apache-2.0 | ano | TS lint/typy | jen **už nainstalované** projektové binárky (`node_modules/.bin`); **žádné `npx`** (umí stáhnout balíček), chybí-li → SETUP REQUIRED |
| pip-audit | lokálně nainstalován | Apache-2.0 | potřebuje síť (OSV) | známé zranitelnosti závislostí | POUŽÍT v CI, lokálně N/A bez sítě |

### CRAP
`CRAP(m) = comp(m)² × (1 − cov(m))³ + comp(m)`, kde `comp` je cyklomatická složitost funkce a `cov`
pokrytí jejích **spustitelných příkazů** (z coverage JSON `executed_lines`/`missing_lines`), ne všech
fyzických řádků mezi začátkem a koncem. Vnořené funkce se z rodiče vyčlení; dekorátory a async patří
funkci; TS jen přes ověřené source mapy. Coverage musí pocházet ze **stejného commitu** jako zdroj.
Chybějící nebo nepřiřaditelné coverage = **N/A**, nikdy cov = 0 ani PASS. Ověření ručně spočítanými
případy (100 %, 0 %, chybějící data) před měřením. Plně pokrytá metoda má CRAP = comp; nepokrytá comp² + comp.
Klasický práh 30. Plán: lizard (comp + rozsahy řádků funkcí) × coverage JSON (pokryté řádky) → CRAP
na funkci; **jen report, REVIEW při > prahu, nikdy FAIL ani bezpečnostní verdikt** (TODO). Práh kalibrovat
na reálných datech Agent Lease a jednoho TS projektu.

## 3. Orchestrátory (jen inspirace, nic nepřipojovat)

| Projekt | Licence | Co stojí za převzetí jako myšlenka | Rozhodnutí |
|---|---|---|---|
| **Gas Town** v1.2.1 | MIT | „Refinery“: dávková merge fronta, ověřovací brány na kombinovaném stacku, při selhání bisekce na vadný MR; agenti nikdy nepushují přímo na main. Witness/Deacon dohled nad zaseknutými agenty. | **INSPIROVAT SE** pro C1/C2 (merge jen přes bránu, `review_approved` + QG PASS) |
| **Gas City** | MIT | skládání organizace z „packs“, nástupce Gas Town | VYNECHAT teď (mladý, nestabilní API) |
| **Beads** | MIT | práce jako strukturovaná data v gitu s ID | VYNECHAT (máme SQLite frontu; duplicita) |
| **Crewship** | `crewship-ai/crewship` Apache-2.0 (★5), `i098/Crewship` (★8) | self-hosted kontejnery pro agenty | VYNECHAT (malé, neověřené projekty; jméno nejednoznačné) |

Reverzní inženýrství nebylo potřeba: vše má veřejné zdroje a dokumentaci (OPEN_SOURCE_COMPLIANCE).

## 4. Návrh MVP B7 (k Noxově kontrole, ne implementace)

1. `agent-lease quality check [--repo PATH] [--commit SHA]` – deterministické, bez LLM tokenů, nad
   **izolovaným snapshotem commitu** (git worktree/archiv v tmp), ne nad soubory, které druhý agent mění.
2. Detekce projektu: `pyproject.toml`/`uv.lock` → Python; `package.json` + lockfile → TS/JS; jinak
   **SETUP REQUIRED**. Příkazy jen z repa (CI, `[tool.*]`, `scripts` v package.json) nebo explicitní
   `.agent-lease/quality.toml`; nic neodhadovat.
3. Výsledky: `PASS / REVIEW / FAIL / N/A / SETUP REQUIRED` po kontrolách (testy, lint, build, coverage,
   security, CRAP), JSON + log do `~/.agent-lease/quality/<repo>/<commit>/`, tabulka `quality_runs`.
   Nox UI (review #6) už má na tento kontrakt připravený panel (`docs/QUALITY_GATE_UI_CONTRACT_2026-11-06.md`).
4. Ochrana brány: diff, který mění testy, CI, `.agent-lease/quality.toml` nebo prahy → **REVIEW**,
   nikdy automatický PASS; prahy mění jen člověk (stejně jako `review_grant_cycle`).
5. Napojení: QG PASS + `review_approved` (B6) = podmínka pro budoucí merge v C1/C2.
6. Běh příkazů repa = **spouštění cizího kódu** (conftest, build backend, npm skripty). Cgroup, timeout a
   run-fence bridge řeší jen životnost procesů, **ne** přístup k účtu – nestačí. Detekce příkazů je
   oddělená od důvěry a od spuštění; spouští se jen v sandboxu z §5, jinak SETUP REQUIRED / REVIEW.

## 5. Izolace a bezpečný pilot (doplněno po review Noxe)

### Sandbox pro cizí příkazy – ověřeno na tomto stroji (bubblewrap 0.9.0, 7. 11., revize 2)

Přesný obal: [`b7-0-sandbox/run.sh`](b7-0-sandbox/run.sh), spouštěný jako
`unshare --user --map-current-user --net run.sh <příkaz>`:

- Vnější `unshare` dá vlastní user + net namespace; uvnitř se zapne jen `lo` (testy s lokálními
  sockety), **žádná routa ven**.
- `bwrap --unshare-all --clearenv`: prostředí jen `PATH`, `HOME=/tmp/home`, `TMPDIR=/tmp`,
  `PYTHONPATH=/work/src:/tools/site:/tools/testdeps`, `PYTHONDONTWRITEBYTECODE=1`,
  `COVERAGE_FILE=/out/.coverage`.
- Mounty: `/usr` RO; snapshot commitu → `/work` **RO**; ověřený standalone interpret → `/tools/python` RO;
  site-packages nástrojů → `/tools/site` RO; testovací závislosti → `/tools/testdeps` RO; scratch
  výstup → `/out` RW; `/tmp` soukromý tmpfs. Nástroje se spouští jako `/tools/python/bin/python3 -m …`
  (ne přes venv se zabudovanými absolutními cestami), takže v sandboxu **není ani `/home`**.
- `prlimit --fsize=50 MiB --nproc=256` uvnitř; zvenku `systemd-run --user --scope -p MemoryMax=…
  -p CPUQuota=… -p RuntimeMaxSec=…` pro paměť, CPU a čas (pilot ho zatím nespouští).
- Návratový kód příkazu se zachová (`exec`, žádná roura); výstupy se píší do souborů v `/out`
  (žádné `| head -c`, které by kód ztratilo), jejich velikost hlídá `--fsize`.

Sonda (7. 11., nástroje z `runtime-eb3a4f5`, náš snapshot `HEAD`): kořen jen `bin dev lib lib64 out proc
tmp tools usr work`, `/home` neexistuje, `~/.agent-lease` ani socket brokeru nejsou, odchozí spojení
`Network is unreachable`, zápis do `/work` → `Read-only file system` (EROFS), zápis do `/out` a `/tmp`
funguje, soubor nad 50 MiB → `File size limit exceeded`, návratový kód 7 prošel ven beze změny,
19 testů včetně testu s lokálním HTTP serverem PASS (Python 3.13.18, SQLite 3.53.1).

**Fail closed:** chybí-li `bwrap`/`unshare` nebo sonda selže (síť, viditelný HOME, socket, zápis do
`/work`), QG cizí příkazy nespustí a vrátí SETUP REQUIRED. Žádná změna host sandboxu, služeb ani oprávnění.

### Pilot – jen náš repozitář a předem vypsané příkazy (čeká na OK Noxe)

Cíl: `agent-lease-mcp` @ přesný commit (archiv do scratch), žádné libovolné repozitáře.
1. Nástroje do izolovaného venv ve scratch s pinem verze a hashem wheelu
   (`coverage==7.17.2`, `lizard==1.25.0`), nic globálně.
2. V sandboxu (obal výše, nástroje v `/tools/site`):
   `/tools/python/bin/python3 -m coverage run --branch -m pytest -q -p no:cacheprovider`
   (zapisuje `COVERAGE_FILE=/out/.coverage`),
   `/tools/python/bin/python3 -m coverage json -o /out/coverage.json`,
   `/tools/python/bin/python3 -m lizard --csv src > /out/lizard.csv`;
   výpočet CRAP vlastním skriptem z `/out` (bez sítě), ruční kontrola tří funkcí (100 %, 0 %, chybí).
3. Scannery (pinnuté binárky ověřené checksumem z releasu, ve scratch): nejdřív **syntetický korpus**
   se známými falešnými klíči a negativními kontrolami, teprve pak lokální kopie historie repa.
   Gitleaks i Betterleaks s **vlastním configem z naší cesty**, ignorovat config z cíle,
   **redakce 100 %**, Betterleaks `--validation=false`, bez diagnostického HTTP serveru, vše uvnitř
   sandboxu bez sítě. Měřit: čas, nálezy, falešně pozitivní, přehlédnuté známé nálezy, rozsah refs.
   Ověřit redakci ve **všech** výstupech (stdout, report, log) – do místnosti, reportu ani commitu nikdy
   hodnota.
4. Rollback: smazat jen vlastní scratch; žádná runtime závislost, změna politiky, DB ani služeb.

Piny zdrojů (licence a analyzované soubory): `2026-11-07-b7-0-source-evidence.json` (Nox).

## 6. Další kroky

- [x] Nox: licence ověřeny (review #7, piny v evidence JSON).
- [ ] Nox: OK k pilotu podle §5 (izolace + přesné příkazy).
- [ ] Pilot (izolovaný venv v scratch, ne globálně): coverage + lizard → CRAP report Agent Lease;
      Gitleaks vs. Betterleaks nad historií repa (počet nálezů, falešné poplachy, čas).
- [ ] Baseline: dnes pytest ~3 min, ruff < 1 s; pilot nesmí CI výrazně prodloužit.
- [ ] Teprve pak implementace MVP a E2E (Python + TS, Ada sama / Nox sám / oba).

Zdroje: GitHub API (licence, releases) pro gitleaks/gitleaks, betterleaks/betterleaks, semgrep/semgrep,
semgrep/semgrep-rules, terryyin/lizard, rubik/radon, coveragepy/coveragepy, gastownhall/gastown,
gastownhall/gascity, steveyegge/beads, crewship-ai/crewship; [Semgrep FAQ – licence](https://semgrep.dev/docs/faq/overview);
[Gas Town README](https://github.com/gastownhall/gastown); [Betterleaks jako nástupce Gitleaks](https://vpncentral.com/?p=246688).

## Dodatek 7. 11. 2026 — měření po převzetí Noxem

Výše uvedený audit je historický podklad před pilotem; jeho tehdejší tvrzení nejsou přepisována.
Michal následně schválil nasazení scanneru 9134485 a omezený místní pilot s limity
2 GiB / 100 % CPU / 15 minut na dávku. [Naměřený report](2026-11-07-b7-0-pilot.md)
dokládá coverage/lizard/CRAP i porovnání scannerů nad syntetickým korpusem a 229 commity.
Pilotní obal dostal pouze syntetický passwd/group pro testy bez HOME; všechny původní
hranice zůstaly ověřené. Původní i opravený pokus jsou popsány, bez fingovaného PASS.

Scanner je [nasazený](../PHASE_B_SCANNER_DEPLOY_RECORD_2026-11-07.md). B7 MVP, globální
hooky, dashboard API a TS E2E **ještě nejsou implementované**. Nox je autorem pilotu,
takže jeho nezávislé review zůstává pending; Ada kvůli limitu není kontaktována.
