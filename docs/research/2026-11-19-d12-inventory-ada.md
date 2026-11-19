# D12 inventura – Ada (Claude Code), 19. 11. 2026

Jen čtení, bez obsahu soukromých instrukcí (zdroje jsou pojmenované, ne citované). Druhou polovinu (Codex, `AGENTS.md`) dělá Nox.

## Zdroje, které dnes řídí Adu

| Zdroj | Druh | Co obsahuje (shrnutí) |
| --- | --- | --- |
| systémové instrukce Claude Code | vestavěné | čtení kódu před změnou, minimální diff bez nevyžádaných funkcí, ověření před tvrzením „hotovo“ |
| globální `~/.claude/CLAUDE.md` | uživatel | koordinace agent-lease (room, claim, release, nerespektovat obcházení hooku) |
| paměť (feedback) | uživatel | ověřovat důkladně a E2E, commit po úkolu, čeština |
| plugin ponytail (hook SessionStart) | plugin | „nejmenší úplná změna“, žádné abstrakce navíc, kontrola dopadu, test u netriviální logiky |
| skill `finish-the-job` | skill | definice Done předem, testy a lint dokola až do zelené |
| skill `senior-architect` | skill | zpochybnit požadavek, vyvážit složitost, kompromisy explicitně |
| skill `agent-design` | skill | deterministický kód před LLM |
| hooky agent-lease (PreToolUse guard, context, Stop, SessionStart) | hook | vynucený edit-gate, doručení zpráv, potvrzení převzetí |
| agent-lease review | služba | autor neschvaluje sám sebe, max 3 kola, pak člověk |

## Čtyři zásady roadmapy proti dnešku

| Zásada | Pokrytí | Mezera / konflikt |
| --- | --- | --- |
| 1. Nejdřív porozumět | systémové instrukce, senior-architect, ponytail („Before you write“) | žádná mezera; vynucení není (jen instrukce) |
| 2. Nejjednodušší dostačující | ponytail (silně), systémové instrukce | **napětí** s pamětí „vždy do hloubky, blbuvzdorně“. Řeší se výkladem: hloubka ověření, ne velikost změny. Nesepsané. |
| 3. Jen relevantní změny | ponytail, systémové instrukce, edit-gate (vynuceně jen cizí soubory) | žádný nástroj neměří nesouvisející změněné řádky. D11 a review ho zčásti nahrazují. |
| 4. Doložený výsledek | finish-the-job, paměť (E2E), systémové instrukce | „co nešlo ověřit a proč“ je v ponytail (poslední řádek), jinde ne vždy |

## Návrh (nezapisuje se bez Michalova souhlasu)

Pro Adu **nepřidávat nové pravidlo**. Všechny čtyři zásady už pokrývají ponytail, skills a systémové instrukce. Další text by duplikoval autoritu. Jediný návrh je jedna věta do globálního `CLAUDE.md` k napětí ze zásady 2:

```diff
+ „Do hloubky“ znamená důkladně ověřit, ne dělat větší změnu: diff drž co nejmenší.
```

Pilot (baseline proti pravidlu na stejných úlohách, kontrola nesouvisejících změn přes `git diff --stat` a D11) spustíme jen s Michalovým souhlasem, protože stojí tokeny obou agentů. Když se přínos neprokáže, výsledek jen archivujeme.
