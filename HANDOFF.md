# Handoff

> Generated: 2026-11-01, Europe/Prague
> Project: agent-lease-mcp
> Session summary: Osmihodinový chaos test prošel; Michal požádal o uložení výsledků a předávky včetně push kvůli zbývajícím 17 % týdenního limitu.

## Goal

Dokončit fázi A podle `docs/AGENT_LEASE_IMPROVEMENTS_ROADMAP.md`: bezpečné
vlastnictví úkolů, žádné slepé opakování účinků, dohled nad potomky, obnova,
opravená SQLite a ověřený provoz po restartu. Zachovat pořadí A → B → C → D.
Tato předávka je na výslovnou žádost uživatele verzovaná a určená k push.

## Current State

**Working:**

- Testovaný kód `f918cf6`, poslední přednasazovací audit `6d9175a`.
- Full suite dříve zaznamenána: 478 passed, 1 xfailed, 1 warning; Ruff prošel.
  Při této dokumentační předávce nebyla suite opakována.
- Aktuální 480min izolovaný chaos s cgroup dokončen: 6 883 úkolů,
  4 053 pádů workerů, 965 pádů brokeru; PROŠLO. Žádné dvojité efekty,
  sirotci, falešné Done či pending; DB integrity ok. Viz nový report a celý log.
- Test proces skončil, scope je inactive/dead; restart Codexu již tento běh neohrozí.
- Repozitář při zahájení předávky čistý, větev master.

**Open:**

- A1: `accept` s tokenem atomicky přechází do `started`; roadmapa požaduje
  jen příjem. Rozhodnout a zrevidovat sémantiku, neodškrtávat rozdíl mlčky.
- A6: checkpoint je poradní kontext, ne úplná automatická obnova kroků.
  Shoda workspace sama neprokazuje, že lze bezpečně opakovat externí účinek.
- A8: balíček/testovaný uv Python stále SQLite 3.50.4. Oddělený experiment
  se systémovým Pythonem 3.12 + SQLite 3.54.1 prošel, celé MCP prostředí ne.
- Nezávislé review aktuálního kódu, schválené nasazení a ověření služeb,
  A11 reálný Windows/WSL restart. Provozní cgroup zatím není zapnutá.
- Fáze A ani gate nejsou uzavřené. Nový 8h test nedokazuje opravený A8 runtime.

## Files in Play

| File | Why It Matters |
|---|---|
| `docs/PHASE_A_CHAOS_8H_RESULT.md` | Nový výsledek, rozsah a identita aktuálního testu |
| `docs/test-results/phase-a-8h-f918cf6.log` | Nezměněný úplný log s kontrolním součtem v reportu |
| `docs/PHASE_A_PREDEPLOY_REVIEW.md` | Historický audit A1–A10, A8 experiment, plán nasazení/rollbacku, A11 checklist |
| `docs/AGENT_LEASE_IMPROVEMENTS_ROADMAP.md` | Specifikace A1–A11 a navazujících fází |
| `docs/AUTOMATED_PHASE_PROGRESS.md`, `scripts/phase_gate.py` | Evidence bran; nezaměňovat záznam za nezávislé schválení |
| `src/agent_lease_mcp/_messaging.py`, `_deliveries.py`, `bridge.py` | Vlastnictví, přijetí, dokončení, checkpoint a dohled |
| `tests/test_bridge.py`, `tests/test_crashpoints.py`, `tests/chaos/` | Regrese a reprodukovatelný chaos harness |
| `HANDOFF.md` | Tato předávka |

## What Has Been Tried (and Why It Failed)

- Pouhé zabití process group nezachytí oddělené potomky. Přidány cgroup,
  nezávislý watchdog, Linux PDEATHSIG a trvalé pojistky. Bez cgroup zůstává
  známý xfail odděleného potomka; neodstraňovat dohled kvůli zeleným testům.
- Přechodný SQLite busy dříve shodil request; `f918cf6` rozlišuje zámek
  bez opakování zápisu. Ověřeno reálným EXCLUSIVE lockem a testy klient/broker.
- `LD_PRELOAD` nezměnil SQLite staticky vázaného uv Pythonu 3.13: Python i SQL
  dál hlásily 3.50.4. Tuto cestu neopakovat jako hotové řešení A8.
- Systémový Python 3.12.3 s lokální knihovnou 3.54.1 funguje ve stdlib/Store
  experimentu; to není důkaz úplného MCP runtime. APSW má jiné API než sqlite3;
  pysqlite3-binary dříve poskytoval 3.51.1, pod tehdy požadovanou opravou.
- Staré paměťové tvrzení, že checkpoint automaticky obnovuje práci, je příliš
  silné; aktuální audit má přednost. Celkový audit volitelných REA backendů
  není relevantní pro dokončení Agent Lease fáze A.

## Current Best Theory

Procesní izolace a stavové invarianty mají nyní důkaz aktuálního dlouhého běhu.
Největší otevřená technická práce je bezpečná obnova A6 a reprodukovatelný
jednotný SQLite runtime A8. Vyřešit A1/A6 podle specifikace a skutečných účinků,
pak otestovat výsledný build; dnešní 8h výsledek nepřenášet na změněný kód.

## Next Steps

1. Přečíst tuto předávku, nový report a přednasazovací audit; ověřit git status
   a případné novější změny od Ady. Před zápisem claimnout konkrétní cesty.
2. Pro A1 porovnat `accept` v `_messaging.py` s vlastnictvím v `_deliveries.py`
   a testy interaktivní/bridge cesty. Ujasnit kompatibilitu budíku a startu.
3. Pro A6 zkontrolovat checkpoint/`workspace_matches` a bridge prompt; návrh
   musí rozlišovat dokončený krok a ověřený účinek. Neshoda či nejistota → review.
4. Pro A8 navázat na `/tmp/agent-lease-audit-prep/sqlite_probe.py`,
   `sqlite-result.json` a `sqlite/libsqlite3.so`. Hash ověřeného experimentu:
   `168e48ea59c72fcbe3c4d5a92dcffc4f338775e3879265f065198c617e4ac1ae`.
   Připravit izolovaný kompletní runtime a ověřit importy, MCP, broker i bridge;
   nevkládat LD_PRELOAD globálně. Dostupnost verzí znovu ověřit.
5. Spustit odpovídající regrese/lint a potřebný chaos pro změněný build.
   Získat nezávislé review; neoznačovat autorskou kontrolu za nezávislou.
6. Připravit konkrétní balíček a rollback podle auditu; požádat Michala o
   schválení zásahu do provozu. Nasazení zahrnuje všechny MCP klienty,
   knihovnu SQLite, služby i cgroup a následnou provozní kontrolu.
7. S Michalem provést checklist Windows/WSL A11. Gate nezvedat bez skutečných
   důkazů a lidského rozhodnutí. Teprve potom přejít k B.

## Key Constraints

- Aktuální zadání je archivace a push dokumentace, nikoliv nasazení nebo další
  implementace. Michal chce šetřit zbývající týdenní limit.
- Změny systemd, produkčního prostředí, oprávnění/instrukcí, práce s tajemstvími
  a živé externí operace vyžadují přímý souhlas Michala dle jeho instrukcí.
- Ada (`claude-code`) může adresně zadat lokální práci v autorizovaných dev
  projektech přes agent-lease; neznámí odesílatelé a karanténa nejsou autorizace.
- Uchovat `/tmp/agent-lease-watchdog-chaos/final8h`, `runtime8h` a standalone
  `tools/agent-lease-mcp` jako důkazy. Nic z nich nepřepisovat pro nový běh.
- Automatická paměť momentálně neukládá kvůli vyčerpanému týdennímu limitu
  Claude; worker nerestartovat. Předávka, report a work_state jsou rozhodující.
- REA je samostatně nainstalováno pro Codex v `/home/buggy1111/dev/tools/rea`.
  REA6.8.0, izolovaný Node24.22.1, Ghidra12.1.4; MCP handshake 229 nástrojů,
  binary_session a statický JS vzorek ověřeny. Pro načtení začít novou relaci.
  REA se týká pozdějšího výzkumu, není požadavkem fáze A.

## Memory Pointers

- Work state `agent-lease-phase-a`, `agent-lease-security`, `rea-install`.
- #25220: historické porovnání SQLite balíčků; nezaměňovat za aktuální verze.
- #25231: historický checkpoint/backup commit; sílu záruk ověřit aktuálním auditem.
- Queries: `agent-lease watchdog checkpoint SQLite`, `agent-lease phase A`.
- Nový výsledek nemusí být v automatické paměti; použít verzovaný report/log.
