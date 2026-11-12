# Oponentura návrhu fáze C

12. 11. 2026, Nox. Posouzen [návrh z 11. 11.](../PHASE_C_DESIGN_PROPOSAL.md) na `8457758567866deb9983f5f214c540abdd424f7e`. Výsledek je návrh úprav dokumentace, nikoli povolení implementace nebo aktivace.

**C1a lze hodnotit samostatně; zápisový pilot C1c zatím nepouštět.** [C0](2026-11-12-c0-codex-worktree.md) vyvrátil předpoklad, že samotný `workspace-write` poskytuje požadované „jen worktree“, a ukázal konflikt mezi ochranou git metadat a commitováním agentem.

## Nutné úpravy návrhu

| Místo | Problém a důsledek | Co má návrh výslovně stanovit |
|---|---|---|
| 3.4 izolace | Mimo-worktree `/tmp` zápis prošel. Čtení mimo repo také. | Oddělit pracovní kopii, privátní scratch, hostitelský stav a síť. Záruky dokazovat sondou finální konfigurace; samotný přepínač nestačí. |
| 3.5 git worktree | Agent nemohl zapisovat index; společný `.git` pod `/tmp` přitom zapisovatelný byl. Sdílené refs/config/hooks nejsou izolovaná historie. | Neotvírat common dir agentovi. Rozhodnout samostatný klon versus commit důvěryhodným procesem; v obou případech chránit origin a ověřit artefakt. |
| 3.6 otisk main | HEAD a pracovní změny nepokrývají veškerá metadata, ignorované soubory a okolní cesty. Main má zároveň zůstat volný pro interaktivní práci, která otisk legitimně změní. | Prevence zápisů na OS hranici je podmínka. Otisk je dodatečný signál, ne důkaz izolace. Při souběžné změně main zastavit publikaci a nevydávat ji automaticky za zásah agenta. |
| 3.6 allowed paths | Kontrola po běhu nebrání vykonání souboru dočasně změněného mimo allowed a následně vráceného. | Rozlišit kontrolu výsledného diffu a omezení runtime oprávnění. Všechny testy a nástroje z kandidáta považovat za nedůvěryhodný kód. |
| 3.3 lidský souhlas | Agent se stejným přístupem k tokenu/DB nebo lidskému endpointu může schválení obejít. | Před zápisovým pilotem zvolit skutečnou hranici lidské identity. Neřešit jen zákazem v promptu; při spuštění znovu ověřit politiku a přesný schválený pokus. |
| 4 regresní test | Samotný nonzero baseline může být import error, chybějící závislost či timeout. Nový test na baseline zatím vůbec neexistuje. | Přenést schválený nový test na původní produkční základ v odděleném běhu, držet důvěryhodnou konfiguraci runneru a rozeznat očekávané selhání aserce od poruchy prostředí. Review musí posoudit i smysl testu. |
| 4 další kola | Nové worktree z poslední hlavy nesmí resetovat původní základ schválení a rozsah bezpečnostního scanu. | Oddělit startovací commit dalšího kola od neměnného původního review base. Brána kontroluje kumulativní změny; přenos důkazů vždy na přesné hashe a politiku (lekce B7/F2). |
| 5 spotřeba | Chybějící usage není bezplatný pokus; cache pole mají u klientů jiné sčítání. Model vyžádaný aliasem nemusí být skutečný model. | Evidovat každý spuštěný pokus i jako unknown. Ukládat požadovaný model odděleně od doloženého skutečného. Aktivní limit vyžaduje příslušnou známou metriku nebo zásah člověka. |
| 5 autorita usage | Vlastní lease není oprávnění vymýšlet měřenou spotřebu. | Záznam bridge musí být rozlišitelný od hlášení agenta; první podvržená hodnota nesmí uzamknout účtování. |
| 5 start a failover | Kontrola pouze při rezervaci neřeší pozdější STARTED, součet podle příjemce resetuje task budget při předání. | Atomická kontrola každé cesty startu, součet úkolu napříč příjemci, denní součet podle agenta; neukončovat rozpracovaný zápis jen kvůli dosažení limitu. |

## Aktualizace stavu předpokladů

- Review #11 je schválené na `120a20c` a Ada oznámila nasazení. Původní tabulka „čeká na Noxe“ je zastaralá.
- B7/#12 je schválené Adou na `0455529`; opravná větev obsahuje schválený F2 fix `a854924`. Nox jej nesloučil do main ani neaktivoval.
- C1a/#13 je po opravě schválené na `32cb343`: revalidace politiky při STARTED a zobrazení skutečného repo/base/allowed před souhlasem. To neřeší OS hranici lidské identity.
- C3a/#15 na `8c5eb16` a C3b/#16 na `a8cfd23` dostaly požadavek změn. Přesné reprodukce jsou ve společném nočním stavu; schválení jejich budoucích oprav musí být na novém plném hashi.
- Nox je aktivní; tvrzení o nečtení zpráv od 10. 11. neodpovídá současnému stavu.

## Pořadí další práce

Dokončit C3 opravy a nezávislá review. Doplnit návrh o rozhodnutí hranice identity a git izolace. Následně samostatný C0 pokus finálního obalu; teprve po jeho přijetí a Michalově souhlasu realizovat C1b/C1c. C2 začíná s funkčními stropy a důkazy baseline/result. Schválené review kódu samo o sobě není povolení nasazení.

V této oponentuře nevznikl runner, sandbox wrapper ani smyčka oprav. Zbývá ověřit finální OS řešení a schválení provozních limitů; dnešní C0 má pouze dvě syntetická lokální uspořádání.
