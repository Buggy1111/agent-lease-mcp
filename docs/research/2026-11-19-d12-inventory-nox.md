# Inventura D12 pro Codex (Nox), 19. 11. 2026

Inventura porovnává čtyři zásady [roadmapy](../AGENT_LEASE_IMPROVEMENTS_ROADMAP.md#d12--podrobné-zadání-karpathy-inspired-pravidla-pro-adu-a-noxe) s instrukcemi aktivní relace, globálním `AGENTS.md`, aktivními skills a hooky Codexu. V repozitáři posuzovaném na `df6b143` ani v jeho relevantních nadřazených adresářích jsem další `AGENTS.md` nenašel. U hooků jsem zkontroloval události a roli příkazů. Soukromé znění instrukcí a hodnoty konfigurace do reportu nepřebírám.

## Jak jsou zásady pokryté

| Zásada | Dnešní pokrytí | Mezera nebo hranice |
| --- | --- | --- |
| Nejdřív porozumět | Instrukce Codexu a aktivní ponytail vyžadují práci s aktuálním kontextem, kontrolu souvisejícího kódu a ověření předpokladů. Agent Lease dodává stav práce a zadání kolegy. | Rozsah a kritéria ověření závisí na úsudku agenta. Hook je nekontroluje. |
| Nejjednodušší dostačující řešení | Ponytail a pravidla pro kód upřednostňují nejmenší úplnou změnu a opětovné použití existujícího řešení. | Při spěchu se může zaměnit malý diff za malé ověření. Pokrytí regresí a hranic oprávnění se posuzuje podle dopadu změny. |
| Jen relevantní změny | Pravidla Codexu omezují změny na rozsah zadání. Agent Lease chrání sdílené cesty a podporuje nezávislé review. | Přidělená cesta sama neurčuje, zda změna uvnitř souboru patří do zadání. To ověřuje diff a review. |
| Doložený výsledek | Instrukce Codexu vyžadují ověření běžnou cestou a uvedení neověřených částí. Broker váže nezávislý verdikt ke konkrétnímu commitu. | U dílčích testů je potřeba uvést jejich rozsah, aby se nezaměnily za ověření běhu v provozu. Pro D12 zatím chybí srovnatelné měření pilotu. |

## Co vynucují hooky

`PreToolUse` používá guard pro koordinaci zápisu do souborů. `SessionStart` a `UserPromptSubmit` doplňují kontext místnosti. `Stop` a `SessionEnd` uvolňují nájmy relace. Jde o ochranu spolupráce. Přiměřenost změn, výběr testů a správnost výsledku hodnotí agent a nezávislý recenzent. [ADR o vynucení nájmů](../adr/0001-vynuceni-hookem.md)

## Návrh a pilot

Všechny čtyři zásady už pokrývají aktuální instrukce Codexu. Doporučuji ponechat je v jejich současných zdrojích. Pokud Michal bude chtít krátké společné znění pro Adu a Noxe, stačí jedna věta do návrhu k jeho schválení.

> Drž změnu v nejmenším úplném rozsahu a ověření zvol podle dopadu změny.

Případný pilot může použít stejné zadání, výchozí commit, kritéria přijetí a dostupné nástroje pro obě varianty v oddělených kopiích. Nezávislé review porovná nesouvisející změny, opravy, úspěšnost testů, čas a tokeny. Pilot ani úprava instrukcí nejsou součástí inventury a vyžadují Michalovo schválení.
