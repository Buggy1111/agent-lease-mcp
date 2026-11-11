# Fáze B – schválení vlastníkem a přechod do fáze C

**Datum:** 11. 11. 2026 · **Zapsala:** Ada (Fable 5.2, dočasný koordinátor).

## Rozhodnutí

Michal 11. 11. 2026: **„fázi B schvaluji a posuneme se dál“.**

Tím vlastník uzavírá fázi B a povoluje zahájit fázi C. Jeho rozhodnutí nahrazuje
dřívější podmínku, že B uzavřou až oba agenti; schválení agentů tím nevzniklo a
nikde se jako udělené neeviduje.

## Co z fáze B dobíhá

Tyto body přechod do C neblokují, ale zůstávají otevřené a mají vlastníka:

| Bod | Stav 11. 11. | Kdo |
|---|---|---|
| Verdikt k review #9 (scanner, commit 9134485, běží v produkci) | čeká od 7. 11. | Nox |
| Nezávislé review kandidáta B7 (9134485..66039c0) | probíhá | Ada (Opus 5.5) |
| Opravy z auditu 11. 11. – nálezy 2, 3, 5, 7, 8, 9 | větev `ada/audit-fixes-20261111` | Ada (Opus 5.5), review Nox |
| Opravy z auditu v B7 – sdílená cgroup, `enabled=false`, E2E v CI | zadáno | Nox, review Ada |
| Kontrola `review check` jako první krok nasazení | zadáno | Nox |
| Matcher hooku Claude Code bez `Bash` a `MultiEdit` | čeká na ruční změnu | Michal |
| Aktivace B7 v produkci | neprovedena | jen se samostatným souhlasem Michala |

Produkce dál běží na runtime `9134485`. Schválení fáze není souhlas s nasazením,
změnou služeb, hooků ani s aktivací B7; ty zůstávají podle
[aktivačního plánu](PHASE_B7_ACTIVATION_PLAN.md).

## Fáze C

Podle [roadmapy](AGENT_LEASE_IMPROVEMENTS_ROADMAP.md): C1 úkol „oprava“ v izolovaném
git worktree se zápisovým režimem bridge, C2 smyčka chyba → regresní test → oprava →
testy → review → nejvýš 3 kola → člověk, C3 sledování spotřeby tokenů a stropy.

Začíná se návrhem, ne kódem: zápisový režim bridge mění bezpečnostní model
(dosud běží jen pro čtení), proto návrh projde nezávislým review a Michalovým
schválením dřív, než se cokoli implementuje.
