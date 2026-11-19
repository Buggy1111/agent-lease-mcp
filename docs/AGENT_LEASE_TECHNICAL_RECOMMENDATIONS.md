# Agent Lease MCP – technická doporučení

**Datum:** 29. 10. 2026 · Podklady: [audit](AGENT_LEASE_IMPROVEMENTS_TODO.md),
[konkurence](AGENT_LEASE_COMPETITOR_ANALYSIS.md), [roadmapa](AGENT_LEASE_IMPROVEMENTS_ROADMAP.md).

## 1. Zachovat jádro, neměnit kategorii

Agent Lease je **ochranná vrstva pro rovnocenné agenty** (nájmy vynucené hookem), ne orchestrátor.
Doporučuji tuto identitu zachovat a orchestraci (review, opravy) stavět **nad** stávající frontou
úkolů (`messages` + `deliveries` + `reply_to`), ne vedle ní. Žádná druhá fronta, žádné druhé zámky.

## 2. „Přesně jednou“ – doporučený model

```
pending ──lease_next(CAS, token, deadline)──► leased ──start(token)──► started
   ▲                                             │                       │
   │ deadline vypršel, nic nespuštěno            │                       ├─ checkpoint(token, krok)
   └─────────────────────────────────────────────┘                       ├─ complete(token, výsledek)  [atomicky]
                                                                         ├─ limit/pád → needs_review nebo resume z checkpointu
                                                                         └─ cancel → stop procesu, ne „Done“
```

- **Jeden vlastník provedení** (`execution_owner` + token) pro všechny cesty: bridge i interaktivní session.
  `accept` zůstane jen jako „viděl jsem / beru na vědomí“, oprávnění spustit dává jen lease.
- **Deadline se kontroluje při každém zápisu** (`ack`, prodloužení, checkpoint, dokončení).
- **Dokončení = jedna transakce:** stav `succeeded` + uložený výsledek + zpráva výsledku. Odeslání
  výsledku je idempotentní (podle tokenu), takže pád uprostřed nevyrobí rozpor ani dvojí „Done“.
- **Nejistý `started`** (pád, limit) se nikdy automaticky neopakuje bez checkpointu → `needs_review`.

## 3. Procesy a obnova

- Spouštět worker v **nové skupině procesů** (`start_new_session=True`) a ukončovat celou skupinu
  (SIGTERM → čekání → SIGKILL), vždy ve `finally`.
- Broker: **periodická reconciliace** (už má sweeper vlákno) – vypršené lease, `started` bez živého
  vlastníka, úkoly v limitu.
- **Checkpoint** jako tabulka vázaná na `(message_id, attempt)`: `step_done`, `next_step`, `git_head`,
  `dirty_fingerprint`, `test_evidence` (cesty/hashe), `updated_at`. Obnova jen při shodě pracovní kopie,
  jinak `needs_review` (vzor: checkpoint s identitou PR/stromu u Orky).
- **SQLite:** přejít na interpreter s opravenou SQLite (3.50.7+ / 3.51.3+) pro služby i MCP server;
  přidat `PRAGMA integrity_check` do `doctor` a po crash testech.
- **Web UI přes broker** (dnes píše do SQLite přímo) – sjednotit „jediný zapisovatel“.

## 4. Bezpečnost

- **Read-only zůstává výchozí.** Zápisový režim bridge jen pro typ úkolu „oprava“, jen v izolovaném
  git worktree, jen s lidským schválením typu úkolu (ne každého kroku) a s povinným review.
- **Screening jako povinný preflight** obou cest spuštění; selhání klasifikátoru = nespustit (fail-closed
  pro autonomní běh, fail-open jen pro interaktivní editaci, jak je dnes).
- **Deny-list vynucený v sandboxu klienta/OS**, ne regexem: zápis do `.env*`, `~/.ssh`, `*credentials*`,
  produkčních konfigurací, `git push`, deploy příkazy, síť mimo whitelist. Regex pravidla jen jako druhá vrstva.
- **Nepřebírat** auto-schvalování všeho (Multiagents `approvalPolicy: never`, Omux
  `--permission-mode auto`) ani HTTP broker bez autentizace.
- **Bezpečnostní události** do jedné tabulky (vzor `guardrail_events` z Multiagents): zablokovaná
  editace, karanténa, překročený limit, nouzové zastavení, schválení člověkem.

## 5. Spolupráce a kvalita

- **Review automat** (vzor Multiagents/Orka): `awaiting_review → changes_requested → approved`,
  verdikt vázaný na commit, autor nesmí schválit sám sebe, **max. 3 opravné cykly** trvale na práci.
- **Recenzent nevidí uvažování autora**, jen zadání, diff a výsledky testů (Orka) – nezávislost.
- **Progress-aware nečinnost** (Orka): nový výstup nebo milník resetuje časovač, absolutní strop zůstává.

## 6. Paměť zkušeností

- Začít jednoduše: **SQLite FTS5** tabulka `experiences(symptom, cause, solution, evidence, verified, project)`,
  bez embeddingů. Vyhledat před spuštěním úkolu a přidat do zadání jako **data**, ne instrukce.
- Alternativa: napojit existující **claude-mem**, které Michal už používá – žádná duplicita.
- Ukládat jen ověřené (odkaz na test/commit), redakce tajemství, retence oddělená od zpráv (30 dní nestačí).

## 7. Tokeny a efektivita

- Push místo pollingu (hotovo: budík, `wait()`), `room` zeštíhlený (29. 10.).
- Další úspory: zkrácený kontext v hooku (jen nové a adresné), shrnutí dlouhých výsledků, cache
  screeningu (už je), měření tokenů tam, kde CLI hlásí (`codex app-server` `turn/completed`).

---

## Závěrečné vyhodnocení

**V čem je Agent Lease lepší?**
Jediný **vynucuje** ochranu souborů hookem před zápisem (konkurence má poradní zámky nebo žádné).
Bezpečnější výchozí nastavení (socket 0600, read-only bridge, žádné auto-schvalování), ochrana proti
prompt injection mezi agenty, malý a čitelný kód (6,7 tis. ř. vs 18–40 tis.).

**Kde zaostává?**
Garance jediného provedení má díry (accept, limit, cancel, deadline); chybí checkpoint, nouzové
zastavení, review automat s limitem kol, izolace oprav (worktree), sdílená paměť a sledování tokenů.

**Které funkce konkurence mají největší přínos?**
1. Checkpoint s identitou pracovní kopie (Orka). 2. Review ledger s max. N cykly (Orka).
3. Progress-aware detekce zaseknutí (Orka). 4. Izolace práce v git worktree (Orka).
5. Stavový automat review (Multiagents).

**Co opravit před přidáváním nových funkcí?**
TOP 5 z auditu: vlastník provedení + deadline + žádné opakování po limitu; životnost procesů a atomické
dokončení; reconciliace + checkpoint + opravená SQLite; rezervace `task_paths`; karanténa bridge + nouzové zastavení.

**Jak zvýšit spolehlivost dlouhého autonomního provozu?**
Fáze A roadmapy a jako důkaz **8hodinový chaos test** v izolované kopii (náhodné pády brokeru, bridge,
limity) s kontrolou: žádné dvojí provedení, žádný sirotek, vše buď obnoveno, nebo `needs_review`.

**Jak se mají Codex a Claude Code postupně zlepšovat?**
Sdílená paměť ověřených řešení (D1) + review verdikty jako zdroj („co recenzent opakovaně vrací“)
→ pravidelně promítnout do `AGENTS.md` / `CLAUDE.md` (lidsky schváleně, ne automaticky).

**Jak minimalizovat tokeny a zbytečnou práci?**
Push čekání, štíhlý kontext, checkpoint (neopakovat hotové), limit opravných kol, rezervace cest
(žádná práce, která skončí kolizí), vyhledání dřívějšího řešení před prací.
