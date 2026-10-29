# Agent Lease MCP – modulární systém a provozní režimy (návrh)

**Datum:** 29. 10. 2026 · **Stav:** NÁVRH ke schválení, nic se neimplementuje bez souhlasu.
Podklady: [audit bod 10](AGENT_LEASE_IMPROVEMENTS_TODO.md#10-modularita-režimy-nouzové-zastavení--částečně-hotovo--p2-cancel-p0),
[roadmapa](AGENT_LEASE_IMPROVEMENTS_ROADMAP.md).

> **Podmínka:** Autonomní režim jde zapnout až po dokončení Fáze A roadmapy (přesně jednou, checkpoint,
> nouzové zastavení, karanténa bridge). Do té doby je v UI zamčený s vysvětlením proč.

## 1. Moduly (nad existujícími komponentami, žádné duplicity)

| Modul | Dnes | Základní | Inteligentní | Autonomní |
|---|---|---|---|---|
| `core.messages` – zprávy, úkoly, příjmy, `wait` | ✅ | ✅ | ✅ | ✅ |
| `core.leases` – nájmy + PreToolUse hook | ✅ | ✅ | ✅ | ✅ |
| `core.presence` – přítomnost, limity poskytovatelů | ✅ | ✅ | ✅ | ✅ |
| `core.safety` – UID socket, redakce, `untrusted`, audit, **pauza** | ✅ (bez pauzy) | ✅ | ✅ | ✅ |
| `wake` – rewake Claude, codex-wake | ✅ | volitelně | ✅ | ✅ |
| `screening` – heuristika + Jev | ✅ volitelně | volitelně | ✅ | ✅ povinně |
| `routing` – výběr agenta podle dostupnosti/schopností, failover | ✅ částečně | ✗ | ✅ | ✅ |
| `review` – stavový automat, max 3 cykly | ✗ | ✗ | ✅ | ✅ |
| `memory` – sdílené ověřené zkušenosti | ✗ | ✗ | ✅ | ✅ |
| `bridge.readonly` – automatické read-only úkoly | ✅ | ✗ | ✅ | ✅ |
| `bridge.repair` – opravy v git worktree se zápisem | ✗ | ✗ | ✗ | ✅ (se schválením typu) |
| `checkpoint` – obnova bez opakování | ✗ | — | ✅ | ✅ povinně |

**Základní bezpečnost (`core.safety`) nejde vypnout v žádném režimu.**

## 2. Provozní režimy

| | 🟢 Základní | 🔵 Inteligentní | 🟣 Autonomní |
|---|---|---|---|
| Kdo spouští práci | jen člověk / agent v session | člověk + bridge (read-only) | i automaticky schválené typy úkolů |
| Zápis do kódu | jen interaktivní session | jen interaktivní session | + `bridge.repair` v izolovaném worktree |
| Review | ruční | povinné pro vybrané úkoly | povinné vždy, max 3 cykly |
| Spotřeba tokenů | minimální (hooky bez LLM) | + screening a review | nejvyšší, s rozpočtovým stropem |
| Síť / LLM klasifikátor | nepotřebuje | volitelně | povinně (fail-closed) |

**Uložení režimu:** jeden řádek v DB (`settings(mode, flags, changed_by, changed_at)`), čtený **v téže
transakci** jako převzetí úkolu (`lease_next`) a start. Přepnutí je auditované. Restart režim zachová.

**Bezpečné přepínání:**
- Nahoru (Základní → Inteligentní → Autonomní): jen člověk z webu (přihlášená session), autonomní jen
  pokud `doctor` hlásí splněné předpoklady (Fáze A, opravená SQLite, karanténa bridge).
- Dolů: kdykoli, okamžitě. Rozběhnuté úkoly doběhnou nebo se zastaví podle volby (drain / stop).
- **Agent nikdy nemění režim ani vlastní oprávnění** – API pro změnu režimu není v `METHODS` brokeru,
  jen ve webu s lidskou session.

## 3. Nouzové zastavení

Jedno tlačítko / příkaz `agent-lease stop --all`:
1. Nastaví `paused=1` (kontroluje se v transakci převzetí → žádný nový start).
2. Pošle všem vlastníkům provedení stop; bridge ukončí celou skupinu procesů.
3. Počká na potvrzení zániku (timeout → SIGKILL), rozpracované úkoly → `needs_review` s checkpointem.
4. Zapíše bezpečnostní událost. Obnovení jen člověkem.

Nezávisí na síti ani klasifikátoru. Funguje i když broker neběží (příkaz pak ukončí služby přes systemd).

## 4. Webový panel (rozšíření `webui.py`)

| Sekce | Obsah |
|---|---|
| **Režim** | přepínač Základní / Inteligentní / Autonomní, u zamčeného důvod („chybí: checkpoint, SQLite“) |
| **Moduly** | zapnout/vypnout jednotlivé moduly v rámci režimu (kromě `core.*`) |
| **Agenti** | kdo je tady, stav, limit, co drží (už existuje) |
| **Úkoly** | fronta, `started`, `needs_review`, review kola, checkpoint (krok X/Y) |
| **Ke schválení** | rizikové úkoly z karantény, review eskalace, typy úkolů pro autonomii |
| **Chyby a opravy** | nalezené / opravené / eskalované (z `review` a `bridge.repair`) |
| **Tokeny** | spotřeba podle agenta a úkolu, kde ji CLI hlásí; rozpočet |
| **Historie** | audit automatických operací a bezpečnostních událostí |
| **🛑 Nouzové zastavení** | vždy viditelné, s potvrzením |

Technicky: stávající autentizace (session cookie, kontrola originu, loopback), zápisy **přes broker**
(dnes web píše do SQLite přímo – sjednotit).

## 5. Bezpečnostní pravidla platná ve všech režimech

Agent bez výslovného lidského schválení **nesmí**:
- měnit vlastní oprávnění, režim ani bezpečnostní nastavení,
- vypínat hooky, screening ani audit,
- provádět destruktivní operace (mazání mimo worktree, `git push --force`, drop tabulek),
- zasahovat do produkčních databází a nasazovat (`deploy*`, `db push`, `netlify deploy` …),
- číst ani zapisovat tajemství (`.env*`, `~/.ssh`, `*credentials*`, `*-key`).

Vynucení: sandbox klienta/OS + deny-list v hooku (dvě vrstvy), selhání kontroly v autonomním režimu =
zakázáno (fail-closed).

## 6. Plán implementace (po schválení)

| Krok | Obsah | Testy |
|---|---|---|
| M0 | **Fáze A roadmapy** (předpoklad) | viz roadmapa |
| M1 | Tabulka `settings` + čtení režimu v `lease_next`/start; CLI `agent-lease mode` | restart zachová režim; agent ho přes broker nezmění |
| M2 | Nouzové zastavení (CLI + web) | pauza vs. start; potvrzený zánik procesů |
| M3 | Registr modulů: mapování na existující služby/env/hooky, `doctor` ukáže stav | matice režim × modul bez sítě pro Základní |
| M4 | Web panel: režim, moduly, ke schválení, historie | jen lidská session smí přepnout; audit změny |
| M5 | Inteligentní moduly (review, routing, memory) podle roadmapy B, D | kombinace modulů, žádné smyčky |
| M6 | Autonomní: `bridge.repair` v worktree, rozpočty | hlavní kopie nedotčena; 3 pokusy → člověk |

**Výsledek:** jeden systém, kde Michal jedním přepínačem volí, jak samostatně Codex a Claude Code pracují,
a jedním tlačítkem je kdykoli zastaví.
