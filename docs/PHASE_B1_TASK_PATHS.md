# Fáze B1: rezervace `task_paths` a úklid prošlých nájmů

Stav: implementováno 6. 11. 2026 (Ada), čeká na review Noxe. Nenasazeno.

## Problém

Úkol může deklarovat cesty (`send --path`, MCP `send(paths=…)`). `lease_next` už dřív úkol
přeskočil, když cesty držel **cizí nájem**. Chyběly ale dvě věci:

1. **Rozběhnutý úkol cesty nerezervoval.** Dva úkoly nad stejnými soubory pro různé agenty (nebo
   pro dvě relace stejného jména) šlo převzít současně a oba pak upravovaly stejné soubory.
2. **Prošlé nájmy se před kontrolou neuklízely**, takže prošlý cizí nájem mohl úkol dál blokovat.

## Řešení (rozšíření stávajících zámků, žádná nová tabulka)

- Úkol ve stavu `leased`/`started` s platným `lease_until` se v `_conflicts` chová jako nájem
  svého adresáta nad všemi svými `task_paths` (účel `task #<id>`). Rezervace vzniká a zaniká
  s lease, nepotřebuje vlastní řádky ani úklid. Prošlý lease přestane blokovat hned, ještě než
  ho reconciliace přepne. Úkol v `needs_review` nic neblokuje (rozhoduje člověk).
- Kontrola i převzetí jsou v téže transakci `BEGIN IMMEDIATE` v `lease_next`, takže ze
  souběžných převzetí překrývajících se úkolů vyhraje právě jedno.
- Pro převzetí úkolu blokují i rozběhnuté úkoly **téhož adresáta** (dvě relace stejného jména).
  Pro `claim`/`edit_gate` adresát nad svými rezervovanými cestami pracovat smí, ostatní ne
  (hook úpravu zablokuje, `claim` vrátí konflikt `task #<id>`).
- `holder_of` (nástroj `owner`) rezervaci úkolu ukazuje.
- `lease_next` a reconciliace brokeru mažou prošlé nájmy (`_prune`). `_conflicts` navíc ignoruje
  prošlé nájmy, i když je ještě nikdo nesmazal.

## Testy

`tests/test_task_paths.py` (7): překryv dvou agentů a pořadí po dokončení, souběh 4 převzetí
→ 1 vítěz, druhá relace téhož agenta, blokace `claim`/`edit_gate` + `owner`, konec rezervace
s lease, prošlý cizí nájem neblokuje a je smazán, reconciliace maže prošlé nájmy. Všech 7 na
předchozím kódu selže. Plná sada: 536 passed, 1 xfailed.

## Omezení

- Rezervace platí jen pro deklarované `task_paths`. Úkol bez cest nic nerezervuje (stejně jako dosud).
- Bash z workeru hook nehlídá (beze změny, viz globální pravidla).

## Po review Noxe #2 (výkon)

Snímek nájmů a rezervací se v `lease_next` načte **jednou** pro všechny kandidáty (`_held`),
ne pro každý čekající úkol znovu pod zápisovým zámkem. Benchmark Noxe
(`scripts/benchmark_nox_b1_reservations.py`): 1000 aktivních + 1000 blokovaných
7,2 s → 0,6 s, 1 dotaz na rezervace. Regrese `test_lease_next_loads_reservations_once`.
