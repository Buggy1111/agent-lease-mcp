# Agent Lease MCP – analýza konkurence

**Datum:** 29. 10. 2026 · **Stav Agent Lease:** HEAD `828a872`, 380 testů, refaktoring dokončený a nasazený.
**Metoda:** repozitáře staženy jen ke čtení (`git clone --depth 1`, bez instalace a spuštění),
prošel se skutečný kód, ne jen README. Nic se nepřebíralo.

| Projekt | Repozitář | Licence | Jazyk, velikost | Testy | Poslední commit |
|---|---|---|---|---|---|
| **Multiagents** | [qanh10x10/multiagents](https://github.com/qanh10x10/multiagents) | MIT (`package.json`) | TypeScript/Bun, ~18 000 ř. | ~155 | 2026-04-26 |
| **Orka** | [Dusttoo/orka](https://github.com/Dusttoo/orka) | MIT (`LICENSE`) | Python + bash, ~40 000 ř. | 654 | 2026-10-15 |
| **Omux** (dříve Cliclaw) | [Happenmass/omux](https://github.com/Happenmass/omux) | MIT (`package.json`) | TypeScript/Node, ~20 000 ř. | 1 174 | 2026-07-14 |
| **Agent Lease MCP** | Buggy1111/agent-lease-mcp | MIT | Python, ~6 700 ř. | 380 | 2026-10-29 |

> Pozn.: pod jménem „Orka“ existuje víc projektů (orka-agents/orka pro Kubernetes, ugorur/orka,
> 12darko/orka). Vybrán Dusttoo/orka, protože jediný řeší Claude Code + Codex s MIT licencí.
> `Mr-Neutr0n/orka` z výsledků hledání už neexistuje.

---

## 1. Architektura v jedné větě

| Projekt | Model |
|---|---|
| **Multiagents** | Centrální **orchestrátor** (MCP server pro Claude Desktop) spouští tým agentů, broker = HTTP na `127.0.0.1:7899` + SQLite. Codex řídí přes `codex app-server` (JSON-RPC), Claude přes push kanál, Gemini „piggyback“. |
| **Orka** | **Supervizor sprintu** nad Jira tikety: každý tiket v **izolovaném git worktree**, pak oddělený code-review a security-review agent, merge jen když jsou všechny brány zelené. Stav v transakčním event store (SQLite). |
| **Omux** | **Meta-agent (LLM)** řídí CLI agenty přes **tmux**: čte jejich obrazovku jako člověk (spinner, potvrzení, chyby), píše jim prompty a smyčkou pokračuje, dokud není cíl splněný. |
| **Agent Lease** | **Rovnocenní agenti** (Claude Code + Codex) ve sdílené místnosti; koordinace přes **nájmy na cesty vynucené PreToolUse hookem**, broker = jediný zapisovatel SQLite přes Unix socket 0600. Žádný centrální „šéf“. |

Klíčový rozdíl: konkurence **řídí** agenty shora (orchestrátor/meta-agent). Agent Lease **chrání**
spolupráci dvou samostatných agentů zdola (hook nedovolí kolizi). To jsou dvě různé kategorie
a Agent Lease nemá smysl předělávat na orchestrátor; spíš převzít jednotlivé osvědčené prvky.

---

## 2. Srovnání po oblastech

Stavy: **HOTOVO** · **ČÁSTEČNĚ** · **CHYBÍ** · **LEPŠÍ U KONKURENCE** (u Agent Lease).

| Oblast | Multiagents | Orka | Omux | Agent Lease | Stav AL |
|---|---|---|---|---|---|
| **Komunikace agentů** | zprávy přes broker, Codex `turn/steer` uprostřed tahu (<3 s) | přes supervizor, ne přímo | meta-agent ↔ agent přes tmux | adresované zprávy/úkoly, příjmy (seen/taking), `wait()` (`_messaging.py`, `server.py`) | HOTOVO |
| **Předávání úkolů** | orchestrátor přiděluje sloty a role | fronta tiketů, závislosti Jira | MainAgent volí agenta „podle schopností“ | `send(kind=task)` + `lease_next`, bridge `codex exec` | HOTOVO (bez routování podle schopností) |
| **Rezervace souborů** | zámky + „ownership zones“, **jen poradní** (agent musí sám volat `acquire_file`, žádný hook) | **git worktree na tiket** (kolize nevzniknou) | samostatné adresáře, žádné zámky | **nájmy vynucené PreToolUse hookem**, i spouštěné skripty a zápisy z Bashe (`guard.py`, `edit_gate`) | HOTOVO – **nejsilnější** |
| **Obnova po pádu agenta** | auto-restart s „handoff“ kontextem, limit 5 restartů (`constants.ts`) | relaunch s limitem, obnova worktree a PR z checkpointu | `resume <id>` agenta, monitor čeká na pane | lease vyprší → `pending`/`needs_review`, žádné dvojí provedení (`_deliveries.py`) | ČÁSTEČNĚ (úkol se neprovede 2×, ale práce se znovu nespustí sama s kontextem) |
| **Pokračování po limitu** | ne (jen časový „budget“) | „continuations“ bez spotřeby relaunch limitu | ne | provider state, čekání na reset, failover (`_availability.py`, `rate_limits.py`) | HOTOVO |
| **Detekce zaseknutí** | idle monitor, `turn/interrupt` Codexu | **progress-aware timer** (výstup a milníky resetují nečinnost) + absolutní strop | detekce stavu z obrazovky | `overdue`, vypršení lease, `needs_review` | ČÁSTEČNĚ – **lepší u Orky** |
| **Ochrana proti smyčkám** | limit restartů | **max. 2 opravné cykly** review (`review-ledger.py`), stropy útrat | strop auto-continue (`main-agent.ts`) | jen omezené retry | CHYBÍ |
| **Rozdělování práce** | role, plán, sloty | Jira závislosti, rozklad velkých tiketů | LLM podle schopností | task `paths` + scheduler přeskočí blokované | ČÁSTEČNĚ |
| **Automatické hledání a oprava chyb** | role „reviewer/QA“ | implement → review → security review → merge-on-green | TDD smyčka meta-agenta | ne (jen ruční zadání) | CHYBÍ |
| **Nezávislá kontrola druhým agentem** | stavový automat `done_pending_review → addressing_feedback → approved` | **oddělený reviewer, nevidí uvažování implementátora** | Codex reviduje diff Claude Code | děláme ručně přes úkoly (Ada ↔ Nox), bez stavového automatu | ČÁSTEČNĚ – **lepší u Orky/Multiagents** |
| **Sdílená paměť** | key-value `knowledge` na session (`broker.ts`) | „reusable product decisions“ | **dvouvrstvá paměť** (globální + projekt), SQLite FTS + embeddingy (`memory/store.ts`) | ne (Michal používá externí claude-mem a MEMORY.md) | CHYBÍ |
| **Bezpečnostní audit** | role-practices (OWASP v promptu) | samostatný security-review agent jako brána | ne | Jev guard + heuristika prompt injection, redakce tajemství, `untrusted` značení (`jev*.py`, `hygiene.py`) | ČÁSTEČNĚ (chrání zprávy, ne kód) |
| **Bezpečnost provozu** | **auto-schvaluje všechny požadavky Codexu**, `approvalPolicy: "never"`; HTTP broker bez autentizace (každý lokální proces) | **merge-guard hook**, root-owned „operator authority“, branch protection | Codex `--ask-for-approval never`, Claude `--permission-mode auto`; web s tokenem pro LAN | socket 0600 jen pro vlastníka, fail-open/fail-closed, bridge **read-only**, odesílatelé úkolů whitelist | HOTOVO – **bezpečnější než Multiagents a Omux** |
| **Monitoring / web** | TUI + web dashboard, 5 záložek | CLI + diagnostika startu | web chat, PWA | web UI (`webui.py`: místnost, odeslání, retry, cancel, SSE), `doctor` | HOTOVO |
| **Spotřeba tokenů** | sleduje tokeny ze `turn/completed` | stropy útrat na tiket/sprint | **push čekání – nula volání LLM při čekání** | hooky bez LLM; `room` zeštíhlen (29. 10.); budík bez tokenů | ČÁSTEČNĚ (nesledujeme spotřebu) |
| **Dlouhý autonomní běh** | budget minut, pauza/resume session | navrženo na celé sprinty (durable supervisor) | auto-continue s limitem | služby systemd, failover; **chybí checkpoint práce** | ČÁSTEČNĚ |

---

## 3. V čem je Agent Lease lepší

1. **Vynucení, ne dobrá vůle.** Jako jediný blokuje kolizní editaci hookem *před* zápisem, včetně
   spuštěného skriptu a zjevných zápisů z Bashe. Multiagents má zámky jen poradní, Omux žádné.
2. **Bezpečnější výchozí stav.** Konkurence pouští agenty bez schvalování (`approvalPolicy: never`,
   `--permission-mode auto`) a Multiagents má broker na HTTP bez autentizace. Agent Lease má socket
   0600, bridge jen read-only a whitelist odesílatelů úkolů.
3. **Ochrana proti prompt injection mezi agenty** (Jev, heuristika, značení cizího obsahu) – nikdo
   z konkurence zprávy mezi agenty takto neprověřuje.
4. **Přesně jednou.** Lease token, atomické převzetí, `needs_review` místo slepého opakování.
5. **Malý a čitelný** (~6 700 řádků vs. 18–40 tisíc), bez závislosti na Jira/tmux/vlastním LLM.

## 4. Kde zaostává

1. **Žádný checkpoint rozpracované práce** – po pádu nebo limitu se ví *že* úkol nedoběhl,
   ne *kde* skončil (Orka má checkpointy a obnovu z PR/worktree).
2. **Žádný stavový automat kontroly výsledku** s limitem kol (Orka: max 2 opravné cykly).
3. **Detekce zaseknutí jen přes vypršení lease**, ne podle skutečného postupu (Orka: progress-aware).
4. **Bridge umí jen číst** – Nox nemůže autonomně opravit chybu (zápisový režim chybí).
5. **Žádná izolace pracovního prostoru** (worktree) pro autonomní opravy.
6. **Žádná sdílená paměť zkušeností** uvnitř systému.
7. **Nesleduje spotřebu tokenů** ani nemá rozpočtové stropy.

## 5. Co převzít (myšlenky, ne kód)

| Odkud | Co | Proč |
|---|---|---|
| Orka | **progress-aware čas**: výstup/milník resetuje nečinnost + absolutní strop | odliší „pracuje dlouho“ od „zasekl se“ |
| Orka | **review ledger s max. N opravnými cykly**, pak člověk | zabrání nekonečnému vzájemnému opravování |
| Orka | **git worktree pro autonomní práci** + brána před merge | izolace oprav, žádný zásah do pracovní kopie |
| Orka | **checkpoint s identitou** (commit/větev/hash stromu) pro obnovu | navázání bez opakování hotové práce |
| Multiagents | **stavový automat úkolu** `working → done_pending_review → addressing_feedback → approved` | srozumitelná kontrola výsledku |
| Multiagents | **guardrail události** (tabulka překročení limitů) | audit autonomních zásahů |
| Omux | **push čekání bez tokenů** – už máme budík a `wait()`; rozšířit | úspora tokenů |
| Omux | **paměť: SQLite FTS** (bez embeddingů na začátek) | vyhledání dřívějších řešení bez další služby |

**Nepřebírat:** auto-schvalování všeho (Multiagents, Omux), HTTP broker bez autentizace,
čtení obrazovky tmux (křehké), závislost na Jira.

## 6. Licence

Všechny tři projekty jsou MIT. Případné převzetí **kódu** (ne jen myšlenky) vyžaduje zachovat
copyright notice a text licence. Doporučení: převzít jen návrhové vzory a implementovat vlastní,
kód nekopírovat.

## Zdroje

- [qanh10x10/multiagents](https://github.com/qanh10x10/multiagents) – `broker.ts`, `shared/constants.ts`, `orchestrator/codex-driver.ts`, `adapters/base-adapter.ts`
- [Dusttoo/orka](https://github.com/Dusttoo/orka) – `hooks/hooks.json`, `scripts/merge-guard.sh`, `scripts/review-ledger.py`, `scripts/durable_liveness.py`, `scripts/event_store.py`, `docs/`
- [Happenmass/omux](https://github.com/Happenmass/omux) / [omux.sh](https://omux.sh/) – `src/agents/codex.ts`, `src/agents/claude-code.ts`, `src/memory/store.ts`, `src/core/main-agent.ts`, `src/server/index.ts`
- [awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators) (přehled dalších nástrojů)
