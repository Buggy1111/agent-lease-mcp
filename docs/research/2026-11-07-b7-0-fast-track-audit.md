# B7.0 – fast-track audit před Quality Gate (7. 11. 2026, Ada)

Read-only průzkum podle `docs/AGENT_LEASE_IMPROVEMENTS_ROADMAP.md` (B7.0) a `TODO.md` (Priorita B).
Nic se neinstalovalo, žádný pilot ještě neběžel. Licence a verze ověřeny přes GitHub API
(`gh api repos/…`, 7. 11. 2026). Čeká na nezávislé ověření Noxem (licence, bezpečnost, přínos).

## 1. Co už máme (nestavět podruhé)

| Kontrola | Kde | Poznámka pro B7 |
|---|---|---|
| Testy | `pytest` (586 testů, ~3 min), CI `ci.yml`, matice Pythonů | Zdroj výsledku **Testy** |
| Lint | `ruff check src tests` v CI | Zdroj **Lint**; formát (ruff format) se nekontroluje |
| Build | `release.yml` (sdist + wheel), lokálně `uv build` ověřen 7. 11. | Zdroj **Build**; v CI zatím jen při release |
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
| **lizard** | 1.25.0 | vlastní permisivní text (GitHub: NOASSERTION; registry uvádějí MIT) | ano | cyklomatická složitost Python + **TS/JS** jedním nástrojem | **POUŽÍT po ověření licence Noxem** |
| radon | (poslední push 2024-10) | MIT | ano | složitost jen Python | VYNECHAT (lizard pokrývá i TS, radon stagnuje) |
| **Gitleaks** | v8.30.1 (2026-03-21) | MIT | ano (Go binárka) | tajemství v celé historii repa | **INSPIROVAT SE / pilot** – jeho autor přešel na Betterleaks |
| **Betterleaks** | v1.10.0 (2026-11-04) | MIT | ano | nástupce Gitleaks, drop-in | **PILOT spolu s Gitleaks** (čerstvý projekt → ověřit stabilitu, falešné poplachy) |
| **Semgrep CE** | v1.184.0 | engine LGPL-2.1, pravidla Semgrep Rules License v1.0 (NOASSERTION) | ano s lokálními pravidly | SAST | **INSPIROVAT SE**: engine volat jako externí nástroj jen s **vlastními** pravidly; registry pravidla jen pro interní použití, nedistribuovat. Rozhodnout po Noxově licenční kontrole |
| eslint (systémový) / tsc | eslint v /usr/bin, tsc chybí | MIT / Apache-2.0 | ano | TS lint/typy | použít **projektové** verze z `package.json` (npx), ne globální |
| pip-audit | lokálně nainstalován | Apache-2.0 | potřebuje síť (OSV) | známé zranitelnosti závislostí | POUŽÍT v CI, lokálně N/A bez sítě |

### CRAP
`CRAP(m) = comp(m)² × (1 − cov(m))³ + comp(m)`, kde `comp` je cyklomatická složitost metody a `cov`
pokrytí řádků/větví té metody (0–1). Plně pokrytá metoda má CRAP = comp; nepokrytá comp² + comp.
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
6. Běh příkazů repa = spouštění cizího kódu → stejná izolace jako bridge (cgroup, timeout, bez sítě
   kde jde), čtení výsledků fail closed.

## 5. Další kroky

- [ ] Nox: nezávisle ověřit licence (lizard text, Semgrep Rules License, Betterleaks), bezpečnost
      spouštění repo příkazů, přínos vs. duplicita.
- [ ] Pilot (izolovaný venv v scratch, ne globálně): coverage + lizard → CRAP report Agent Lease;
      Gitleaks vs. Betterleaks nad historií repa (počet nálezů, falešné poplachy, čas).
- [ ] Baseline: dnes pytest ~3 min, ruff < 1 s; pilot nesmí CI výrazně prodloužit.
- [ ] Teprve pak implementace MVP a E2E (Python + TS, Ada sama / Nox sám / oba).

Zdroje: GitHub API (licence, releases) pro gitleaks/gitleaks, betterleaks/betterleaks, semgrep/semgrep,
semgrep/semgrep-rules, terryyin/lizard, rubik/radon, coveragepy/coveragepy, gastownhall/gastown,
gastownhall/gascity, steveyegge/beads, crewship-ai/crewship; [Semgrep FAQ – licence](https://semgrep.dev/docs/faq/overview);
[Gas Town README](https://github.com/gastownhall/gastown); [Betterleaks jako nástupce Gitleaks](https://vpncentral.com/?p=246688).
