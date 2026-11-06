# Fáze B5: detekce zaseknutí podle postupu

Stav: implementováno 6. 11. 2026 (Ada), čeká na review Noxe. Nenasazeno. Mění dohled
nad procesy v bridge, proto **před nasazením 15minutový chaos test** (čtení výstupu je nově
po kouscích).

## Problém

Bridge měl jediný limit `--timeout` (výchozí 1800 s) od spuštění příkazu. Dlouhý, ale aktivní
úkol (velké review, dlouhé testy) skončil jako „timed out“, zatímco zaseknutý příkaz zbytečně
blokoval worker celých 30 minut.

## Řešení

- `--max-runtime` (nebo `AGENT_LEASE_MAX_RUNTIME`) zapíná nový režim: `--timeout` je pak limit
  **nečinnosti** a `--max-runtime` absolutní strop (nikdy kratší než `--timeout`).
- Postup = cokoli z: nový výstup příkazu (čte se po kouscích), **nový** checkpoint úkolu
  (starší checkpoint z doby před během se nepočítá), aspoň 1 s CPU za 30 s v delegované cgroup
  (`cpu.stat usage_usec`, jen s `--cgroup`).
- Bez postupu po dobu limitu nečinnosti: proces se ukončí celou skupinou/cgroup a výsledek
  zní `stalled: no progress for N s (output, checkpoint or CPU) after M s`. Další zpracování
  (needs_review u started úkolu) je stejné jako u timeoutu.
- **Bez `--max-runtime` je chování stejné jako dosud** (jen absolutní `--timeout`). Callable runner
  (OpenRouter) má dál jen absolutní limit.

Doporučené nasazení: `AGENT_LEASE_MAX_RUNTIME=3600` v `~/.config/agent-lease/environment`
(aktivní úkol až 1 h, nečinný se ukončí po 30 min jako dosud).

Pozor: `claude -p --output-format text` během práce nic nevypisuje. Bez `--cgroup` (CPU) a bez
checkpointů ho tedy hlídá jen limit nečinnosti = dnešní `--timeout`, takže nic se nezhorší, jen
se mu neprodlouží. Produkční bridge běží s `--cgroup`.

## Testy

`tests/test_progress.py` (9): aktivní příkaz přežije limit nečinnosti, tichý je ukončen jako
stalled, strop platí i pro aktivní, vnější postup (checkpoint) drží tichý příkaz, beze změny
bez `idle_timeout`, mapování voleb v Bridge, jen nové checkpointy, čtení `cpu.stat`, CPU jako
postup. Plná sada: 551 passed, 1 xfailed.

## Po review Noxe #4

Vzorkování postupu (checkpoint, CPU) běží aspoň 3× za limit nečinnosti (`min(30 s, limit/3)`),
CPU práh se úměrně zmenší. Krátký limit tak neuplyne dřív než první vzorek. Regrese Noxe
s produkčním intervalem v `tests/test_nox_review_regressions.py`. 15min chaos snímku eefc55c
s `AGENT_LEASE_MAX_RUNTIME=600` a `--cgroup`: **PROŠLO** (331 úkolů, žádné dvojité efekty,
přeživší procesy ani visící úkoly, integrita ok).
