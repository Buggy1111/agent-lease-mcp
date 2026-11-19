# Fáze B3 + B4: review jako stavový automat se stropem opravných kol

Stav: implementováno 6. 11. 2026 (Ada), čeká na nezávislé review Noxe. Provozně **nenasazeno**:
nasazení = nový připnutý runtime podle `PHASE_A_DEPLOY_PLAN.md` a Michalův souhlas.

## Co to řeší

Dosud se review domlouvalo volnými zprávami. Nebylo jasné, na jaký commit verdikt platí, autor mohl
„schválit“ sám sebe a opravné smyčky mezi agenty neměly konec.

## Model

Tabulka `reviews` (jeden řádek = jedno review jednoho tématu v jednom repu) a `review_events`
(každý přechod: kdo, kdy, z jakého stavu do jakého, na jaký commit, s jakou poznámkou).
Retence je nemaže.

```
           request(commit)                      verdict approved
 working ──submit──▶ awaiting_review ───────────────────────────▶ approved ──close──▶ closed
                        ▲      │ verdict changes_requested           │
                        │      ▼                                     │ submit(nový commit)
                 submit │  changes_requested                         ▼
              (= kolo+1)└──────┘                              awaiting_review
                               │ changes_requested při kolech == max
                               ▼
                           escalated ──(člověk: +1 kolo)──▶ changes_requested
                               └──────(člověk: uzavřít)──▶ closed
```

## Pravidla (vynucuje `Store`, ne agent)

| Pravidlo | Jak |
|---|---|
| Verdikt je vázaný na commit | `verdict` vyžaduje plný hash (40/64 hex). Jiný než naposledy předložený `head_commit` → odmítnuto („stale verdict“). `HEAD`, větve a zkratky se nepřijímají, aby recenzent nemohl omylem schválit commit, který neviděl. |
| Starý verdikt ≠ nový diff | Nový commit po schválení schválení zruší a review se vrací do `awaiting_review`. Stejný commit znovu = beze změny. |
| Autor neschválí sám sebe | `author == reviewer` je odmítnuto při založení i při verdiktu. Je-li určen recenzent, verdikt dá jen on. |
| Max. 3 opravná kola (B4) | Každé `submit` po `changes_requested` je kolo. Žádost o změny po 3. kole → `escalated` a vzkaz člověku i autorovi. Z `escalated` agent nic nepředloží ani neuzavře. Počítadlo je v SQLite, restart procesu ani brokeru ho nevynuluje. |
| Další kolo jen od člověka | `review_grant_cycle` (+1 kolo, povinný důvod) a `review_human_close` nejsou v `METHODS` brokeru. Volá je jen web UI, které má přímý přístup k DB. Agentní metody odmítají rezervovaná jména (`michal`, `human`, `broker` …), takže se agent za člověka nevydá ani přes argumenty brokeru. |
| Strop nejde obejít zavřením | Autor sám zavře jen `approved` (sloučeno) nebo `working` (nic nepředloženo). Rozběhnuté review zavře jen člověk, jinak by close + nové review téhož tématu vynulovalo kola (Nox #655/P1, regresní test). |
| Jedno otevřené review na téma | Druhé `request` stejného tématu ve stejném repu je odmítnuto, dokud první není `closed`. |
| Dotaz „je to schválené?“ | `review_approved(repo, commit)` → schvalující review jen pro přesně tento commit (i po uzavření po sloučení). Podklad pro C2 a pro budoucí bránu před pushem. |

## Rozhraní

- **MCP:** `review(action=request|submit|close|show|list|check, …)` a `verdict(review_id, verdict, commit, note)`.
  Repo určuje agent parametrem `repo` (jinak adresář, kde relace začala; ten se během relace
  nemění, proto je parametr nutný — zjištěno živým testem po nasazení). `submit` nejdřív ověří autora a pak vyžaduje, aby
  cwd bylo repo review: git se nikdy nespouští v cestě ze sdílené DB (cizí repo by přes svou
  konfiguraci mohlo spustit kód). Git běží s `core.fsmonitor=false`, `core.hooksPath=/dev/null`,
  `--no-optional-locks`, `--no-replace-objects` a bez globální a systémové konfigurace
  (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM`, `GIT_ATTR_NOSYSTEM`). Definuje-li repo
  filtr nebo diff program, `status` se nespustí (clean filtr by ho jinak zavolal) a nástroj
  rovnou varuje před možnými necommitnutými změnami. Cestu `repo` volí agent, který git
  v ní může spustit i sám; pojistka chrání před tím, aby ho k tomu nevědomky dotlačil cizí repozitář. Necommitnuté
  změny → `warning`: review pokrývá jen commit.
- **Vzkazy:** každý přechod pošle `chat` dotčenému (recenzentovi při předložení, autorovi po verdiktu,
  člověku při eskalaci). Úmyslně ne `task`, aby se zatím nic nespouštělo samo. Automatické review
  bridgem patří do C2.
- **Web:** sekce Review v levém sloupci: stav, `kolo oprav n/3`, poslední výhrady. Eskalované nahoře
  červeně, s tlačítky „Povolit ještě 1 kolo“ a „Uzavřít review“.

## Hranice a známá omezení

- Důvěra v identitu je stejná jako u zbytku agent-lease: broker je socket 0600 téhož uživatele.
  Proces se stejnými právy může zapisovat do DB přímo. Automat chrání před chybou a zkratkou agenta,
  ne před škodlivým procesem téhož uživatele.
- Store neověřuje, že commit v repu existuje (nespouští git). Ověřuje to MCP nástroj při převodu na
  hash. Přímé volání přes broker s neexistujícím hashem projde, ale verdikt pak musí přijít na
  stejný hash.
- Autor může strop obejít novým tématem se stejnými commity. Je to vidět v historii a ve webu, ale
  automat to nebrání. Vazba rozpočtu na řadu commitů může přijít s C2.
- Brána „bez schváleného review se nepushne / nesloučí“ zatím není. `review_approved` je na ni připravené (C2/B6).

## Testy

`tests/test_reviews.py` (16, vč. close/reopen a souběhu verdiktů i submitů), `test_server.py::test_review_tools_bind_to_commits` (skutečné git repo),
`test_broker.py::test_review_state_machine_over_broker_and_human_methods_refused`,
`test_webui.py::test_human_review_actions_only_in_web_ui`. Plná sada: 521 passed, 1 xfailed.
