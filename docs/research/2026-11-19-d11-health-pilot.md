# Pilot D11 `agent-lease health` na agent-lease-mcp – 19. 11. 2026, Ada

Příkaz: `agent-lease health .` (123 souborů Pythonu; skryté adresáře jako `.claude/worktrees`, `.venv` a build se přeskakují).

| Nález | Počet | Vyhodnocení |
| --- | --- | --- |
| Duplicitní implementace | 1 | `tests/test_quality.py:38 repo` = `tests/test_quality_runs.py:24 repo`. **Skutečný duplikát** (fixtura gitového repa v testech). Kandidát na sdílenou fixturu v `conftest.py`, nízká priorita. |
| Kandidáti na mrtvý kód | 0 | Po zapojení CLI. Před zapojením hlásil právě nové, ještě nevolané funkce D8–D11, tedy správně. |
| Cykly importů (při načtení) | 0 | Líné importy uvnitř funkcí se nepočítají. Úmyslně lámou cykly Store ↔ mixiny ↔ broker. Ve verzi, která je počítala, vyšly 2 komponenty (8 a 2 moduly), oba **falešné poplachy**. |
| Názvy mimo snake_case | 0 | Metody se nehodnotí (`do_GET`/`do_POST` diktuje `BaseHTTPRequestHandler`). Předtím 2 falešné poplachy. |

## Falešné poplachy a slepá místa

- Mrtvý kód je jen **kandidát**. Jako použití se počítá i výskyt jména v řetězci, kvůli `getattr` a dynamickým tabulkám metod (`broker.METHODS`). To snižuje falešné poplachy, ale skryje mrtvou funkci, jejíž jméno náhodou stojí v textu. Kód volaný jen z jiného balíčku nebo pluginu bez řetězcové stopy se nahlásí chybně.
- Duplikáty: porovnává se struktura těla bez názvů. Funkce, které se liší jen konstantou, se za duplikát nepovažují.
- GitNexus se v pilotu nepoužil. Bez Michalova souhlasu se další nástroj neinstaluje.

## Závěr

Nástroj je levný (sekundy), neměnný a na tomto repu má po úpravách heuristik 1 pravdivý nález a 0 falešných poplachů. Doporučení: spouštět ho jako podklad pro review (B3/B6), ne jako bránu.
