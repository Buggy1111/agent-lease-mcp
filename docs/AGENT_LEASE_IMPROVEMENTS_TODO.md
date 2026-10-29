# Agent Lease MCP – technický audit bodů 1–10

**Datum:** 29. 10. 2026 · **Ověřený stav:** HEAD `828a872` (refaktoring dokončený, nasazený, 380 testů)
**Kdo:** audit kódu Nox (Codex, úkol #535, read-only, závady reprodukované v izolované SQLite `:memory:`
a mockách), doplnění a ověření Ada (Claude Code). Žádná změna kódu ani provozní DB.

## Verdikt

Základ koordinace (nájmy vynucené hookem, fronta s lease tokenem, příjmy, failover) je hotový a funguje.
**Michalův hlavní požadavek – několik hodin bez dozoru, správná obnova a NIKDY tentýž úkol dvakrát – dnes
splněný není.** Jsou konkrétní díry ve vlastnictví úkolu, opakování po limitu, rušení běhu a karanténě.
Před jakoukoli novou funkcí (review, opravy, paměť, režimy) je potřeba opravit body P0 níže.

| # | Oblast | Stav | Priorita |
|---|---|---|---|
| 1 | Automatické zotavení po pádu | ČÁSTEČNĚ HOTOVO | **P0** |
| 2 | Ochrana proti dvojímu spuštění | **VYŽADUJE OPRAVU** | **P0** |
| 3 | Ukládání stavu práce (checkpoint) | CHYBÍ | **P0** |
| 4 | Rozdělování úkolů | ČÁSTEČNĚ HOTOVO | P1 |
| 5 | Kontrola výsledků druhým agentem | ČÁSTEČNĚ HOTOVO | P1 |
| 6 | Bezpečnostní režim autonomních agentů | **VYŽADUJE OPRAVU** | P2 (karanténa bridge P0 před autonomií) |
| 7 | Automatické hledání a oprava chyb | CHYBÍ | P1 (po P0) |
| 8 | Sdílená paměť zkušeností | CHYBÍ | P2 |
| 9 | Automatický bezpečnostní audit změn | CHYBÍ | P1 (před autonomními zápisy) |
| 10 | Modularita, režimy, nouzové zastavení | ČÁSTEČNĚ HOTOVO | P2 (sémantika cancel P0) |

---

## 1. Automatické zotavení po pádu — ČÁSTEČNĚ HOTOVO · P0

**Existuje:** systemd `Restart=on-failure` pro broker a bridge (`deploy/systemd/*.service`), trvalá
aktivace workeru (`_deliveries.py:32–62`), klient čeká na restart brokeru bez opakování odeslaného zápisu
(`broker.py:176–217`), backoff bridge (`bridge.py:341–354`), `leased` → `pending` a `started` po vypršení →
`needs_review` (`_deliveries.py:99–115`), nájmy vrací Stop hook, tvrdý pád řeší TTL.
Testy: `test_remote_store_waits_for_a_restarting_broker`, `test_expired_started_task_needs_review` a další.

**Mezery:**
- Vypršení úkolů se kontroluje až v `lease_next` daného adresáta; broker sweeper řeší jen failover a chaty.
  Když je agent v limitu, přerušený `started` zůstane dlouho `started`.
- **Kritické:** výjimka v `on_tick` opustí běh bez `finally` → původní dítě (`codex exec`) může běžet dál,
  zatímco bridge začne další kolo (`bridge.py:127–146`, reprodukováno). Timeout zabije jen přímý proces,
  ne celou skupinu potomků.
- `needs_review` je bezpečné zastavení, ne obnova práce. Chybí kontrola git HEAD, rozpracovaných souborů
  a návaznosti nájmů.
- **SQLite 3.50.4** (služby i MCP server, ověřeno 29. 10.) má známou chybu WAL-reset; `doctor` jen varuje.
- **WSL / restart Windows:** `deploy/windows/start-bridge.ps1` je neověřený návrh, nespouští `codex-wake`,
  nekontroluje návratové kódy `wsl.exe`.

**Navrhované opravy:** pravidelná reconciliace úkolů v brokeru (rozšíření stávající expirace); správa
skupiny procesů, `finally` úklid a potvrzení zániku původního vlastníka před dalším pokusem; přechod na
opravenou SQLite (≥ 3.50.7 nebo 3.51.3) + test integrity; ověřit Windows start.

**Testy:** skutečný SIGKILL bridge/brokeru v izolované DB ve fázích: před `started`, po `started`, během
subprocesu, mezi uložením výsledku a `ack` → žádný přeživší potomek, žádné druhé spuštění. Expirace viditelná
i při `provider=limited`. Windows: restart PC / logon / `wsl --shutdown` → služby zpět.

## 2. Ochrana proti dvojímu spuštění — VYŽADUJE OPRAVU · P0

**Hotové:** `BEGIN IMMEDIATE` + náhodný lease token (`_deliveries.py:93–159`), token při `ack`,
`STARTED` před spuštěním, `dedupe_key` + unikátní index, bridge respektuje interaktivní převzetí,
failover přesouvá jen `pending` bez převzetí. Testy `test_store.py:164–207`, `test_failover.py`.

**Závady (všechny reprodukované Noxem):**
- **(a)** `accept` bez aktivního lease nevytvoří vlastníka; dvě interaktivní session stejného jména
  mohou obě „brát“ tentýž úkol; přijmout lze i hotový (`succeeded`) úkol (`_messaging.py:206–226`).
- **(b)** `ack` ani `extend_lease` nekontrolují `lease_until > now` → prošlý lease jde oživit; bridge ignoruje
  neúspěšné prodloužení a pokračuje i po zrušení (`_deliveries.py:176–198,264–276`, `bridge.py:279–283`).
- **(c)** **Po limitu (429) se rozběhnutý úkol slepě zopakuje celý** (`started` → `failed` → `retry` →
  `pending`, `bridge.py:312–325`). CLI mohl před limitem část práce už udělat.
- **(d)** Výsledek „Done“ se posílá **před** `ack` a výsledek `ack` se ignoruje → po zrušení přijde „Done“;
  pád mezi odesláním a `ack` nechá rozporný stav (`bridge.py:309–311`).
- **(e)** Budík Codexu zapisuje kurzor až po `codex queue` → pád mezi tím zopakuje probuzení.

**Oprava:** jeden vlastník provedení (execution owner + token) pro interaktivní i bridge cestu; `accept`
oddělit od oprávnění spustit; kontrola deadline v `ack`/prodloužení, neúspěšné prodloužení = zastavit
proces a nepřijmout výsledek; po `started` + limitu obnova z checkpointu nebo `needs_review`, nikdy slepé
opakování; dokončení a výsledek atomicky s tokenem, odeslání výsledku idempotentní.

**Testy:** závod (bariéra) dvou session stejného jména + bridge + failover → jeden vlastník; prošlý token bez
sweepu selže; cancel / neúspěšný renew zastaví proces; 429 po zaznamenaném efektu efekt nezopakuje;
kill mezi send a ack → žádný rozpor ani dvojí výsledek.

## 3. Ukládání stavu práce (checkpoint) — CHYBÍ · P0

Schéma ukládá text, stav, pokusy, token, poslední chybu, příjmy a kurzory (`store.py:56–185`), **ne** hotový
krok, další krok, výsledky testů ani stav gitu. Bridge po obnově spouští znovu původní zadání
(`bridge.py:68–72,274–289`), soubor s poslední odpovědí se po běhu ztrácí.

**Oprava:** verzovaný checkpoint vázaný na pokus a vlastníka: poslední hotový krok, další krok, git HEAD +
otisk rozpracovaných souborů, odkazy na diff/artefakty a výsledky testů. Ukládat před předáním / limitem;
při obnově nejdřív ověřit shodu pracovní kopie. Nezakládat druhou frontu.

**Testy:** restart po každém kroku → hotový krok se neopakuje; cizí/starý token checkpoint nepřepíše;
změna HEAD/souborů → review; výsledky testů přežijí restart.

## 4. Rozdělování úkolů — ČÁSTEČNĚ HOTOVO · P1

**Hotové:** přítomnost a stavy poskytovatelů, řetězy failoveru, `task_paths` + priority, atomické nájmy.

**Mezery:** `lease_next` cesty úkolu jen porovná s nájmy, **nerezervuje je** → dva úkoly se stejnou cestou
dostanou oba lease (reprodukováno). `_conflicts` čte i prošlé nájmy (chybí `_prune`). Schopnosti agentů
nejsou typované; `--to auto` může vybrat i servisní identitu (bridge/wake).

**Oprava:** v transakci `lease_next` uklidit prošlé nájmy a atomicky rezervovat workset úkolu; směrování
filtrovat od služeb, minimální popis schopností.
**Testy:** dva souběžné `lease_next` s překrytím cest → jeden vítěz; nezávislé cesty paralelně;
`auto` nikdy na `codex-wake`/`*-bridge`.

## 5. Kontrola výsledků druhým agentem — ČÁSTEČNĚ HOTOVO · P1

**Existuje:** volitelná kontrola shody výsledku se zadáním přes Jev → `needs_review`; ruční retry/cancel.
Jev hodnotí text, ne diff a testy; při nedostupnosti výsledek nezamítne.
**Chybí:** samostatný recenzent, stavový automat schválení, **limit opravných kol**; `retry` nemá strop,
limit 12/h je v paměti a restart ho vynuluje.
**Oprava:** povinný nezávislý review pro vybrané úkoly, strukturovaný verdikt vázaný na commit/diff,
**max. 3 opravné cykly** trvale na celé práci, pak člověk.
**Testy:** autor neschválí sám sebe; 3. zamítnutí eskaluje a 4. oprava se nespustí ani po restartu/failoveru;
starý verdikt neschválí novější diff.

## 6. Bezpečnostní režim — VYŽADUJE OPRAVU · P2 (karanténa P0 před autonomií)

**Hotové vrstvy:** bridge read-only (Codex) / plan (Claude), privátní DB a socket s kontrolou UID,
role `untrusted`/`unconfigured`, whitelist odesílatelů úkolů, redakce tajemství, značení cizího obsahu,
heuristika + Jev, tvrdá regex pravidla, log 0600.

**Závady:**
- **Bridge nepoužívá screening před spuštěním** – úkol s rizikem 1.0 se přesto spustil (reprodukováno).
  Karanténa platí jen pro interaktivní kontext.
- Jev shell guard bere jen `Bash` (ne shell Codexu); `init` instaluje jen lease guard, ne jev-guard.
- Lease guard a Jev jsou při chybě fail-open; žádné obecné deny/allow pro DB, `.env`, klíče, produkci, síť.
- Socket ověřuje UID, ne jméno agenta → whitelist chrání proti omylu, ne proti kompromitovanému agentovi.

**Oprava:** povinný screening jako preflight obou cest spuštění; rizikový úkol jen s lidským verdiktem
vázaným na úkol/revizi; vynutit politiku zápisu a sítě v sandboxu klienta/OS. Read-only zůstává základ.
**Testy:** karanténní úkol nespustí runner ani při vypnutém/padlém Jev; zakázaný zápis/síť odmítnuty;
schválení jiného úkolu nelze použít; log neobsahuje testovací tajemství.

## 7. Automatické hledání a oprava chyb — CHYBÍ · P1 po P0

Bridge je jen spouštěč úkolů, výchozí režim read-only; dočasný adresář není izolovaný checkout.
**Návrh:** až po P0 typ úkolu „oprava“ v odděleném git worktree, s worksetem, povinnými testy, review
z bodu 5 a trvalým rozpočtem max. 3 pokusy.
**Testy:** oprava jen v izolovaném checkoutu, hlavní pracovní kopie beze změny; po 3 neúspěších eskalace;
restart rozpočet nevynuluje.

## 8. Sdílená paměť zkušeností — CHYBÍ · P2

Historie zpráv a audit (retence 30 dní) nejsou znalostní báze; bridge do zadání nepřidává dřívější zkušenosti.
**Návrh:** malý typovaný záznam „příznak → příčina → ověřené řešení“ s odkazy na test/commit, nebo
integrace existující paměti (claude-mem); ukládat jen ověřené závěry; zprávy nikdy nepovyšovat na instrukce.
**Testy:** nalezení dřívějšího řešení; neověřené se neprezentuje jako fakt; prompt injection v paměti
zůstává daty; redakce tajemství.

## 9. Automatický bezpečnostní audit změn — CHYBÍ · P1 před autonomními zápisy

Dnes runtime ochrany a unit testy, ne povinná kontrola diffu, závislostí, tajemství a oprávnění.
**Návrh:** povinný bezpečnostní verdikt nad přesným commitem v review cyklu (bod 5), lokální secret scan,
selhání scanneru = neprošlo.
**Testy:** podvržené tajemství, nebezpečná změna oprávnění, zranitelná závislost a rozbitá bezpečnostní
regrese zablokují dokončení; nová revize zneplatní starý verdikt.

## 10. Modularita, režimy, nouzové zastavení — ČÁSTEČNĚ HOTOVO · P2 (cancel P0)

**Dnes jde zvlášť zapnout/vypnout:** broker, bridge (instance), codex-wake, notifier (systemd), lifecycle a
lease hooky, rewake (`init`), env přepínače (`AGENT_LEASE_FAIL_CLOSED`, `_JEV`, `_FALLBACKS`, `_TTL`,
`_CHAT_EXPIRE_HOURS`, `_REQUIRE_BROKER`…), Jev guard `shadow`/`enforce`. Web UI: přihlášený člověk, chat,
úkoly, snapshot + SSE, board, retry, cancel.
**Chybí:** režimy Základní/Inteligentní/Autonomní, správa služeb a hooků z webu, globální **pauza/nouzové
zastavení**. **Cancel nezastaví proces** a „Done“ po zrušení přijde (reprodukováno). Web UI zapisuje
do SQLite přímo, mimo broker.
Návrh viz [AGENT_LEASE_MODULAR_SYSTEM.md](AGENT_LEASE_MODULAR_SYSTEM.md).

---

## TOP 5 před přidáváním nových funkcí

1. **P0** Jeden vlastník provedení (token) pro interaktivní i bridge cestu, kontrola deadline,
   žádné opakování `started` úkolu po limitu; cancel / ztráta lease zastaví proces.
2. **P0** Životnost procesů: zabít celou skupinu potomků, `finally` úklid, atomické dokončení + výsledek,
   žádné falešné „Done“ ani osiřelé procesy.
3. **P0** Reconciliace + checkpoint/obnova + crash testy; opravená SQLite; ověřit restart Windows/WSL.
4. **P1** Rezervace `task_paths` v transakci `lease_next`, úklid prošlých nájmů (rozšířit stávající, ne nové zámky).
5. **P0 před autonomií** Karanténa i pro bridge; trvalá globální pauza / nouzové zastavení s potvrzeným
   ukončením běhů. Teprve potom review/opravy max 3, paměť a další inteligence.

Navazující dokumenty: [roadmapa](AGENT_LEASE_IMPROVEMENTS_ROADMAP.md) ·
[technická doporučení](AGENT_LEASE_TECHNICAL_RECOMMENDATIONS.md) ·
[modulární systém](AGENT_LEASE_MODULAR_SYSTEM.md) · [konkurence](AGENT_LEASE_COMPETITOR_ANALYSIS.md)
