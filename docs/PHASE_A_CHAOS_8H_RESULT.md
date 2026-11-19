# Fáze A: dokončený osmihodinový chaos test

Zapsáno: 1. 11. 2026. **Výsledek: PROŠLO.** Jde o výsledek izolovaného
testu, nikoliv nezávislé review nebo schválení nasazení či celé fáze A.

## Identita a důkazy

- Testovaný commit: `f918cf610d1c0c001c8ac45174680486f18f6548`.
- Snapshot: `/tmp/agent-lease-watchdog-chaos/final8h`; jeho `COMMIT` obsahuje
  uvedený hash. Po doběhnutí porovnáno všech 45 verzovaných souborů pod
  `src/` a `tests/chaos/` s tímto commitem: beze změny. Snapshot neobsahuje
  úplnou unit test suite ani projektové balicí soubory.
- [Úplný log](test-results/phase-a-8h-f918cf6.log), 119 řádků, 14 878 bajtů.
- SHA-256 logu: `a69549f202b52e456247aa5453ea1cc6c9142189eb036e78e7176351c4676816`.
- Původní log: `/tmp/agent-lease-watchdog-chaos/chaos8h.log`.
- Izolovaná data: `/tmp/agent-lease-watchdog-chaos/runtime8h/agent-lease-chaos-rmosuj8n`.
- Systemd scope: `run-rfc1643f4aaee4f2c8790ab1ed2eaec98.scope`;
  po dokončení `inactive/dead`, testovací proces již neběží.

Příkaz původního běhu (není pokyn k opětovnému spuštění):

```sh
systemd-run --user --scope --property=Delegate=yes env -i \
  PATH=/usr/bin:/bin LANG=C.UTF-8 \
  TMPDIR=/tmp/agent-lease-watchdog-chaos/runtime8h \
  PYTHONPATH=/tmp/agent-lease-watchdog-chaos/final8h/src \
  /tmp/agent-lease-watchdog-chaos/tools/agent-lease-mcp/bin/python -u \
  /tmp/agent-lease-watchdog-chaos/final8h/tests/chaos/run_chaos.py \
  --minutes 480 --workers 4 --cgroup --drain-seconds 600
```

## Naměřený výsledek

| Ukazatel | Hodnota |
|---|---:|
| Odeslané úkoly | 6 883 |
| Simulované pády workerů | 4 053 |
| Simulované pády brokeru | 965 |
| Zrušení | 506 |
| Interaktivní převzetí / pády | 1 689 / 867 |
| Úspěšné / k prověření / zrušené | 3 691 / 2 686 / 506 |
| Úkoly se zaznamenaným účinkem | 4 573 |
| Karanténní vstupy | 684 |
| Zaznamenaní potomci workerů | 830 |
| Simulované limity poskytovatele | 273 |
| Zbývající pending / visící leased či started | 0 / 0 |

Žádné dvojité účinky, dvojité ani falešné Done, nesoulad evidovaných úkolů,
spuštění karanténního vstupu, start či účinek během pauzy, přeživší procesy,
potomci po pádu bridge ani jejich následné účinky. Každý `succeeded` měl
právě jeden účinek a právě jedno Done. Limit a následné zotavení byly skutečně
pozorovány; integrita DB `ok`, pokrytí scénářů `ano`. Skutečný provider probe
nebyl proveden. Účinek u přerušeného úkolu není automaticky úspěšné dokončení;
proto je počet účinků vyšší než počet `succeeded`.

## Rozsah závěru a zbývající práce

Test potvrzuje invarianty harnessu s cgroup a simulovanými poskytovateli
v izolované DB. Neprokazuje právě-jednou účinky libovolných externích služeb,
reálné API limity, restart Windows/WSL ani nasazení opraveného runtime.
Opravená SQLite nebyla tímto během dodána do provozních služeb.

Historický [přednasazovací audit](PHASE_A_PREDEPLOY_REVIEW.md) zůstává zachován;
jeho tehdejší čekání na 8h test je tímto výsledkem vyřešeno. Nadále zbývá
dořešení A1/A6, jednotný opravený SQLite runtime A8, nezávislé review,
schválené nasazení s provozní kontrolou a skutečný test A11 Windows/WSL.
Podrobná předávka je v [HANDOFF.md](../HANDOFF.md). Gate fáze nebyl posunut.
