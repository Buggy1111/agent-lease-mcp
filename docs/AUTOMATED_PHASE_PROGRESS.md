# Řízení vývoje A → B → C → D (bezpečný základ)

Stav: **připraveno, VYPNUTO**. Tento dokument a `scripts/phase_gate.py` zavádějí
ověřitelný postup mezi fázemi, **nikoli autonomní spouštění Ady/Noxe**.
Fáze A není potvrzená jako dokončená ani nasazená. V žádném případě z tohoto
dokumentu nevyvozuj souhlas se změnou oprávnění, produkčním nasazením nebo
spuštěním dlouhé autonomní práce.

## Pořadí a podmínky

Použij [roadmapu](AGENT_LEASE_IMPROVEMENTS_ROADMAP.md) jako specifikaci úkolů.
Existující záznamy o testech jsou důkazy pouze v rozsahu, který skutečně pokrývají.
Nepřepisuj historický audit novým tvrzením o dokončení.

- **A:** `phase_a_review` (nezávislá kontrola), `chaos_8h` (aktuální build),
  `windows_wsl_restart` (reálný test na uživatelově PC),
  `process_supervision` (pád bridge + dohled + oddělení potomci),
  `sqlite_fastmcp` (konzistence a migrace), `security` (karanténa, stop,
  izolace oprávnění).
- **B:** `review_state_machine`, `retry_budget`, `diff_security_scan`,
  `routing_and_locks`.
- **C:** `isolated_worktree`, `repair_loop`, `budget_enforcement`,
  `write_mode_human_approval`. Poslední bod vyžaduje **výslovné schválení**
  uživatele, nikoli domnělé schválení ze samotné roadmapy.
- **D:** `shared_memory`, `operation_modes`, `research_agents`,
  `verified_sources`, `research_budget`. Podrobnosti D1–D7 v roadmapě.

## Použití lokálního gate

```bash
python scripts/phase_gate.py --state .agent-lease-phase.json init
python scripts/phase_gate.py --state .agent-lease-phase.json status
python scripts/phase_gate.py --state .agent-lease-phase.json record chaos_8h docs/test-report.md
# --verified zadat až po skutečném nezávislém ověření
python scripts/phase_gate.py --state .agent-lease-phase.json record chaos_8h docs/test-report.md --verified
python scripts/phase_gate.py --state .agent-lease-phase.json advance
# Jen explicitní rozhodnutí člověka a pouze po ověření všech bran:
python scripts/phase_gate.py --state .agent-lease-phase.json advance --confirm
```

`record` zaznamenává **prohlášení** o ověření; program sám důkaz nečte,
neověřuje GitHub status, nespouští testy a nezná identitu zapisující osoby.
`--confirm` není technicky chráněný lidský podpis. Nejde proto o
bezpečnostní hranici proti agentovi se shellovým přístupem; pouze o
konzervativní mechanismus plánování. Neuděluj agentům pravomoc spustit
`--verified` nebo `--confirm` místo tebe.

Při chybějícím souboru, poškozeném schématu nebo chybějících důkazech postup
selže uzavřeně. Přechod nemění kód, nespouští službu ani neuvolňuje sandbox.

## Co ještě chybí k plně autonomnímu běhu

Záměrně **není implementovaný** nepřetržitý scheduler, automatické přidělování
úkolů podle roadmapy, API verifikace důkazů, trvalé schvalování nezávislou
identitou, integrace do brokeru a restart-safe orchestration s atomickým
claimem, stavovým automatem review a checkpointy. Tyto prvky patří k B/C
a zavádějí se až po dokončení A.

Při rozvoji znovu použít existující broker, task leases, checkpointy a
emergency stop; nevytvářet nezávislý druhý worker/scheduler.
