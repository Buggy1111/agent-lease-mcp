# ADR 0001 — Nájmy se vynucují hookem, ne domluvou

**Stav:** přijato, 10. 9. 2026

## Kontext

9. 9. 2026 v repu `silikon-manager` pracovali Claude Code a Codex ve stejném
pracovním stromu. Koordinační soubor `docs/AGENT-HANDOFF.md` existoval a oba
agenti o něm věděli. Ve 22:28 si stejně oba sáhli na
`deploy/make-work-package.sh` — jeden ho editoval, druhý ho zároveň spouštěl.
Bash čte skript průběžně, text se posunul pod běžícím procesem a místo 3,7GB
instalačního balíčku vypadl nepoužitelný 198MB zmetek.

Nikdo nejednal ve zlé vůli. Agent prostě pracoval a na nástěnku se nepodíval.

## Varianty

**A. Nechat to na domluvě** (co dělají AgentRoom, Agent Claim MCP,
agent-orchestration). Levné, ale AgentRoom si ve svém paperu sám dokumentuje, že
agenti doporučující nájmy porušují. Přesně tenhle režim právě selhal.

**B. Izolace přes git worktree** — každý agent vlastní strom, slučuje git.
Silné a ověřené. **Ale v tomhle repu nestačí:** všechno těžké je gitignorované
(`pb_data` 20 GB, `zalohy` 19 GB, `deploy/windows/assets`), takže ve worktree
nejde postavit balíček. Git o těch souborech neví a nemá je jak sloučit.

**C. Vynucení PreToolUse hookem.** Claude Code i Codex mají hooky se stejnou
sémantikou (exit 2 = zablokovat, důvod na stderr), takže jeden skript obslouží
oba a rozhodnutí padne deterministicky na úrovni volání nástroje.

**D. Zámek na úrovni souborového systému** (flock, chmod). Vynucené tvrdě, ale
neví, kdo je kdo, nemá TTL a rozbíjí běžné nástroje.

## Rozhodnutí

**C**, s **B** jako doporučeným doplňkem pro čistě kódovou práci.

Klíčová věta: *koordinace, kterou lze ignorovat, bude ignorována.* Rozdíl mezi
doporučujícím a vynuceným zámkem není v procentech — je v tom, jestli protokol
platí i ve chvíli, kdy je agent zabraný do práce. Což je přesně ta chvíle, kdy
na něm záleží.

## Důsledky

- Sdílený stav musí být čitelný i mimo MCP session, protože hook běží jako
  samostatný proces → SQLite na disku (viz [ADR 0002](0002-sqlite-a-stdio.md)).
- **Fail-open.** Když je koordinace rozbitá, hook pustí dál. Kdyby blokoval,
  první, co kdokoli udělá, je vypnout ho — a pak nechrání nic.
- **Bash se nehlídá.** Zjistit z příkazové řádky, co skript zapíše, je
  nespolehlivé; falešné blokování by agenta naučilo hook obcházet. Na skripty
  sahající na sdílené věci je explicitní `claim`.
- Nájem je **lease s TTL**, ne zámek: spadlý agent nesmí repo zablokovat navěky.
  Cena je okno, kdy je cesta držená mrtvým agentem — přijatelná.
- **Volný soubor si hook vezme sám.** Na explicitní `claim` se pak nedá
  zapomenout. Cena: agent drží i nájmy, o které nežádal — proto krátké TTL.

## Kdy to přehodnotit

Až budou oba agenti běžet v oddělených worktree i pro sdílené artefakty (třeba
proto, že `pb_data` přestane být v repu), většina důvodu pro zámky zmizí a zbyde
jen přítomnost a vzkazy.
