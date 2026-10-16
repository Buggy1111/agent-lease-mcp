# ADR 0003 — Jev je samostatný guard, ne rozšíření lease policy

**Stav:** přijato, 22. 9. 2026

## Kontext

Izy (Hermes agent) sepsal `~/dev/jev-integrace-dokumentace.md` o Jevu —
System One modelu od TypeSafe AI (launch 15. 9. 2026, ověřeno websearch:
reálná firma, reálný produkt, ceny a tvar API sedí). Bod 4.1 navrhuje
použít Jev jako `PreToolUse` security gate: klasifikovat každý Bash
příkaz (`noul` destruktivní?) a při nízké confidence nebo destruktivním
vyhodnocení blokovat.

Michal chtěl tohle přidat do `agent-lease-mcp`, protože tenhle projekt už
`PreToolUse` hook na Bash má.

## Proč to není totéž

`policy.py` má u `WRITE_TOOLS` a `script_target` výslovnou, zdůvodněnou
volbu: **Bash se obsahově nehlídá.** Zjistit z příkazové řádky, co skript
udělá, je nespolehlivé — a falešné blokování by bylo horší než žádné,
protože by agenta naučilo hook obcházet (`ADR 0001`, stejná věta).
Guard je navíc fail-open schválně: rozbitá koordinace nesmí zastavit práci.

Jevův navrhovaný use-case je přesně tohle — obsahová klasifikace Bash
příkazů — jen s jiným nástrojem (pravděpodobnostní model místo regexu).
Cena chyby je stejná: falešný blok učí obcházení, falešný průchod nechrání
nic. Přidat to do `policy.decide()` by navíc smíchalo dva různé problémy
v jedné funkci — „kdo drží soubor" (deterministické, nízká cena chyby) a
„je příkaz nebezpečný" (pravděpodobnostní, vysoká cena chyby na obě
strany) — a rozbilo by to `policy.py`, což je dnes čistá funkce bez IO a
bez síťové závislosti.

## Rozhodnutí

Nový, samostatný modul (`jev_policy.py` / `jev_client.py` / `jev_guard.py`)
a samostatný hook s vlastním entry pointem (`agent-lease-jev-guard`),
zapojený v settings.json vedle lease guardu, ne uvnitř něj. Konzumuje
stejný hook payload (`tool_name`/`tool_input`), ale rozhoduje nezávisle.

Dvouvrstvá struktura:

1. **Natvrdo napsané vzory** (`jev_policy.hard_block`) pro jistá,
   nevratná rizika (`rm -rf /`, force push, `git reset --hard`,
   `curl | bash`, …). Neptají se Jevu, nepotřebují síť, platí vždy, jakmile
   je hook zapojený.
2. **Jev na šedou zónu** — volitelný (`JEV_GUARD_ENABLED`), a i zapnutý
   nejdřív jen loguje (`JEV_GUARD_MODE=shadow`), dokud `enforce` neřekne
   jinak. To je přímo doporučení ze zdrojové dokumentace (bod 6, krok 2) a
   zároveň jediný způsob, jak si práh ověřit na reálném provozu dřív, než
   začne blokovat.

Fail-open zůstává: chyba Jevu (síť, timeout, špatný tvar odpovědi) hook
nikdy nezastaví, jen zaloguje.

## Důsledky

- `policy.py` zůstává čistý a beze změny — nulové riziko regrese
  v tom, co projekt reálně chrání (kolize na souborech).
- Nová síťová závislost (TypeSafe API) je izolovaná v `jev_client.py`,
  takže zbytek testů běží dál bez sítě a bez API klíče.
- Vypnuto ve výchozím stavu Pythonu (`JEV_GUARD_ENABLED=false`), ale
  natvrdo napsané vzory jsou aktivní hned po zapojení hooku do
  settings.json — bezpečné výchozí chování bez nutnosti API klíče.
- Přibývá závislost na externí placené API třetí strany pro cokoli nad
  rámec natvrdo napsaných vzorů — to je vědomý kompromis, ne přehlédnutí.
- Codex-side zapojení (jméno shell nástroje v jeho hook payloadu) není
  ověřené — `_bash_command` reaguje jen na `tool_name == "Bash"`
  (Claude Code). Než se přidá `matcher` do `~/.codex/hooks.json`, je
  potřeba zjistit skutečný tvar payloadu, ne hádat.

## Kdy to přehodnotit

Až bude na logu z `shadow` režimu vidět, jaký práh (`JEV_GUARD_THRESHOLD`)
dává rozumný poměr false positive/negative — pak přepnout na `enforce`.
Revize prahu měsíčně, jak navrhuje zdrojová dokumentace, bod 6, krok 5.
