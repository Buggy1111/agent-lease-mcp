# Quality Gate — pouze prezentační návrh

Michal zadal rozšíření premium UI podle `Kvalitní brána v agentním dashboardu.png`. Práce navazuje na izolovanou větev `nox/ui-redesign-20261106`, nikoli na novou aplikaci.

## Rozsah

Panel na hlavním přehledu, samostatný přehled Quality Gate, detail repozitáře a upozornění v detailu úkolu jsou implementované pouze uvnitř `PAGE_HTML`. Žádné kontroly kódu se nespouštějí. Neexistuje nové API, tabulka, soubor s výsledky, localStorage ani další úložiště. Výsledky se nepřipojují ke snapshotu/SSE a neodvozují z existující tabulky reviews.

Výchozí stav je **Připravuje se · dosud bez výsledků**. Počty jsou `—`, poslední kontrola není známá a tabulka je prázdná. Neznámé údaje se nevydávají za nulu úspěšných kontrol.

Tlačítko **Zobrazit demo** zpřístupní šest jasně označených příkladů `demo/*`: PASS, REVIEW, FAIL, N/A, SETUP REQUIRED a NOT RUN. Jde o pevné ukázkové záznamy pouze v paměti stránky. Režim je označen v záhlaví stránky, v panelu přehledu a v detailu projektu. Reload demo vypne. Stav skutečného úkolu zůstává **NOT RUN — Kontrola zatím není dostupná**, i pokud uživatel prohlíží ukázkové PASS výsledky. Není vazba podle shody názvu repozitáře ani podle posledního review jiného commitu.

## Plánovaný vstup pro budoucí adapter

Následující struktura je **návrh prezentačního rozhraní**, nikoli zavedený API kontrakt:

```json
{
  "repository": "organization/repository",
  "branch": "branch-or-null",
  "commit_sha": "exact-full-hash-or-null",
  "agent": "agent-or-null",
  "timestamp": "ISO-8601-or-null",
  "overall_status": "PASS|REVIEW|FAIL|NA|SETUP_REQUIRED|NOT_RUN",
  "checks": {
    "tests": {"status": "NOT_RUN", "detail": "Textový výsledek"},
    "lint": {"status": "NOT_RUN", "detail": "Textový výsledek"},
    "typecheck": {"status": "NOT_RUN", "detail": "Textový výsledek"},
    "build": {"status": "NOT_RUN", "detail": "Textový výsledek"},
    "coverage": {"status": "NA", "value": null, "detail": "Není dostupné"},
    "security": {"status": "NOT_RUN", "detail": "Textový výsledek"},
    "crap": {"status": "NA", "value": null, "detail": "Není podporováno"}
  },
  "logs": [],
  "reviewer": null,
  "review_status": null,
  "history": []
}
```

`NA` se zobrazuje jako `N/A`, `NOT_RUN` jako `NOT RUN`. Stav dodává budoucí zdroj; frontend z kombinace kontrol sám nepočítá verdikt, prahy, oprávnění ani postup. Protokoly jsou v návrhu reprezentované polem `logs`, ale zatím nejsou připojené a nevznikají nefunkční odkazy. Historie a nezávislé review v demu jsou označené jako ukázkové. Detail má textové výsledky, ne pouze barevné ikony. Tabulku lze fokusovat a posouvat vodorovně uvnitř komponenty.

## Co čeká na fázi B a prověření architektury

1. Prověřit aktuální implementaci review a bezpečnostního verdiktu, skutečné datové zdroje a GitHub TODO. Nepřipojovat UI přímo k tabulkám ani nevytvářet paralelní databázi.
2. Definovat autoritativní Quality Gate model a jednoznačnou vazbu repository + přesný commit/diff + konkrétní úkol. Oddělit bezpečnostní review, povinné kontroly a lidská schválení.
3. Definovat použitelnost kontrol, required/optional pravidla, význam PASS/REVIEW/FAIL/N/A/SETUP/NOT RUN a pravidla agregace. Tyto mechanismy nejsou součástí UI.
4. Získávání výsledků testů, lint/typecheck, buildu, coverage, security a CRAP score: podporované jazyky, nástroje, dostupnost, časová platnost a původ výsledku. Zatím se nic nespouští.
5. Navrhnout a schválit napojení na existující autorizované API/SSE; ověřit rozsah přístupných projektů, historii a bezpečné odkazy na protokoly včetně redakce citlivých dat. Teprve pak implementovat adapter.
6. Navrhnout, jak přesně aktuální výsledek ruší novější commit nebo změněný diff. Poslední PASS jiného úkolu nikdy nesmí působit jako ověření nového úkolu.
7. Nezávislé review Ady a Michalův souhlas před merge/deploy; aktuální backendové změny v main se zde nemergují.

## Dokumentace a pořadí

Byly přečteny `TODO.md` a `docs/AGENT_LEASE_IMPROVEMENTS_ROADMAP.md` v tomto worktree. Jeho výchozí `TODO.md` je starší souhrn z 21. 10. a neobsahuje samostatný hotový Quality Gate kontrakt. Roadmap popisuje A → B → C → D; B3/B4 jsou commitové review a limit kol, B6 bezpečnostní verdikt a secret scan, C2 regresní smyčka. Ani jeden soubor na této větvi neustanovuje kompletní projektový dashboard testy/coverage/CRAP. Michalovo nové zadání proto není vydáváno za již existující backendovou funkci a roadmap ani TODO se tímto patchem nepřepisují.

Ada mezitím pracuje na B1/B2/B5/B6 v hlavním checkoutu. Tato větev zůstává nad d01bf3e; před budoucím sloučením se musí zkontrolovat integrace a možné konflikty ve webui.py. Žádné aktuální lease Ady nebyly měněny.
