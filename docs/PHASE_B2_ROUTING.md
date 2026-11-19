# Fáze B2: směrování bez servisních identit

Stav: implementováno 6. 11. 2026 (Ada), čeká na review Noxe. Nenasazeno.

## Problém

`agent-lease send --to auto` bral jako kandidáty všechny aktivní účastníky místnosti kromě sebe,
tedy i `codex-bridge`, `claude-code-bridge`, `codex-wake`, broker nebo člověka. Úkol pro most nebo
budík nikdo neudělá. Navíc offline agent s běžícím mostem vypadal nedosažitelně, ačkoli jeho
most úkol udělá.

## Řešení (`src/agent_lease_mcp/routing.py`)

- Kandidát je jen skutečný agent. Nikdy `*-bridge`, `*-wake`, rezervovaná jména (`broker`, `human`,
  `michal`, jméno člověka z `human-name`) ani `untrusted-*`/`unconfigured-*`.
- Agent je dosažitelný, když je sám aktivní **nebo** běží jeho most `<agent>-bridge`.
- Jediný dosažitelný agent se vybere hned, bez Jevu. Pro víc kandidátů rozhoduje Jev, pokud je
  zapnutý, jinak CLI odmítne a chce `--to <agent>` (jako dřív).
- Jev dostává krátký popis schopností agentů (výchozí pro Adu a Noxe, přepis přes
  `AGENT_LEASE_CAPABILITIES="codex=…;claude-code=…"`).
- Web UI (výběr „Komu“) mosty a člověka nenabízí už od `7a55579`.

## Testy

`tests/test_routing.py` (6): vyřazení služeb a člověka, agent dosažitelný přes most, popisy
schopností a jejich přepis, předání popisů Jevu, CLI `auto` nikdy na most/budík, odmítnutí, když
jsou kolem jen služby. Plná sada: 542 passed, 1 xfailed.
