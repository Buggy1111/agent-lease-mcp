# Osmihodinový chaos test na `main` 5e8a171 — 20. 11. 2026, Ada

Michal 20. 11. ráno: „dodělejte agent lease, zátěžové testy“. Stejný harness a parametry jako
[8h chaos fáze A](PHASE_A_CHAOS_8H_RESULT.md), tentokrát nad kódem fází A–D, který běží v produkci.

- Snapshot: čistý `git archive 5e8a171caab67fdcf30845fd7cdd093a4f6bf2aa` (= nasazený `runtime-0175752`;
  `79fe207` mění proti němu jen `doctor.py`, který harness nepoužívá).
- Běh: 20. 11. 04:59–13:31, exit 0. Izolovaná DB v `~/dev/work/agent-lease-load-20261120/runtime/`,
  žádný zásah do živé místnosti, služeb ani konfigurace. Předtím 2min smoke PASS.

```sh
systemd-run --user --scope --property=Delegate=yes env -i PATH=/usr/bin:/bin LANG=C.UTF-8 \
  TMPDIR=$W/runtime PYTHONPATH=$W/snap/src $VENV/bin/python -u $W/snap/tests/chaos/run_chaos.py \
  --minutes 480 --workers 4 --cgroup --drain-seconds 600
```

## Výsledek: PROŠLO

| Ukazatel | 20. 11. (A–D) | fáze A (pro srovnání) |
|---|---:|---:|
| Odeslané úkoly | 7 356 | 6 883 |
| Simulované pády workerů | 4 000 | 4 053 |
| Simulované pády brokeru | 989 | 965 |
| Zrušení | 578 | 506 |
| Interaktivní převzetí / pády | 1 672 / 837 | 1 689 / 867 |
| Úspěšné / k prověření / zrušené | 3 462 / 3 316 / 578 | 3 691 / 2 686 / 506 |
| Úkoly se zaznamenaným účinkem | 4 316 | 4 573 |
| Karanténní vstupy | 737 | 684 |
| Zaznamenaní potomci workerů | 751 | 830 |
| Simulované limity poskytovatele | 293 | 273 |
| Zbývající pending / visící leased či started | 0 / 0 | 0 / 0 |

Žádné dvojité účinky, dvojité ani falešné Done, nesoulad evidovaných úkolů, spuštění karanténního
vstupu, start či účinek během pauzy, přeživší procesy, potomci po pádu bridge ani jejich účinky.
Každý `succeeded` měl právě jeden účinek a jedno Done. Limit i zotavení skutečně pozorovány,
integrita DB `ok`, pokrytí scénářů `ano`, žádný pokus o skutečný provider. Souhrn:
[`test-results/chaos8h-20261120/summary.log`](test-results/chaos8h-20261120/summary.log).

Vyšší podíl `needs_review` než ve fázi A není závada: přerušený úkol se záměrně neoznačí za
úspěšný a čeká na člověka. Počty závisí na náhodném rozvrhu harnessu.

## Rozsah závěru

Harness zatěžuje jádro doručování (broker, bridge, nájmy, pauza, karanténa, limity) s cgroup
a simulovanými poskytovateli. Review, fix, research a quality fronta (B–D) má vlastní stavový soak
[`PHASE_BD_SOAK_RESULT_2026-11-20.md`](PHASE_BD_SOAK_RESULT_2026-11-20.md) (review #36/#37).
Neprokazuje skutečné API limity, právě-jednou účinky externích služeb, restart Windows ani ostrý
provoz vypnutých bran B7/C/D3–D7.
