# Akceptační test RE (rozšíření D3–D6) – 19. 11. 2026, Ada

**Stav:** neověřeno, čeká na nezávislou kontrolu Noxe. Jde jen o statickou analýzu, nic se nespouštělo na zařízení ani nic nezapisovalo.

## Artefakt

| | |
| --- | --- |
| Zdroj | `docs/research/d-re-fixture/checksum.c`, SHA-256 `bc6dbd373257376a7c09c592b35139c70c5009e4b063b395546a4162070f9ba1` |
| Překlad | `gcc -O1 -o checksum checksum.c`, gcc 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1), x86_64 ELF |
| Binárka | SHA-256 `5fb3918d1b0fd6a99fdf1c56fad704e12484a688811c7a5da2aab3d1582289ae` (sestavení není bajtově reprodukovatelné mezi stroji; ověřuje se zdroj a chování) |
| Nástroj | REA MCP → Ghidra 12.1.4, import `ephemeral-source-immutable`, výchozí auto-analýza |

## Dostupnost a oprávnění

REA MCP odpověděl a Ghidra provider se načetl (`open_binary` → ELF x86_64). Analýza běží v dočasném projektu, který se po `close_binary` smaže. Hash binárky před analýzou a po ní je stejný. Nástroj původní bajty nezapisuje (limitace v Evidence).

## Zjištění (z Evidence, ne ze zdroje)

- Funkce: `main` @ `0x101189`. Statická funkce `checksum` je **inlinovaná** do `main` (`-O1`), samostatně v seznamu procedur není.
- Importy: `strlen`, `fwrite`, `__printf_chk`, `__libc_start_main`, `__cxa_finalize`.
- Řetězce v `.rodata`: `agent-lease` (výchozí vstup), `input too long\n`, `%08x\n`.
- Pseudokód `main`: bez argumentu použije `"agent-lease"`; jinak `strlen(argv[1]) > 0x40` → `fwrite("input too long\n")` a návrat 2. Jinak smyčka `h = (h ^ byte) * 0x1000193` se startem `0x811c9dc5`. To jsou konstanty **FNV-1a 32bit**. Výstup `printf("%08x\n")`, návrat 0.
- Kontrola chováním: `checksum` → `50c39ac5`, `checksum hello` → `4f9f2cab` (FNV-1a z „hello“ je `4f9f2cab`, sedí).

## Nejistoty

- Názvy proměnných a typy (`undefined8`, `param_1`) jsou odhad dekompilátoru.
- Inlining znamená, že hranici funkce ze zdroje nelze z binárky doložit.

## Možnosti úprav (jen popis, nic se neprovedlo)

Změnit výchozí vstup by šlo patchem řetězce na `0x102004`. **Zápis, patch ani flash se bez Michalova výslovného souhlasu, zálohy a plánu obnovy nedělají.** Tento test nic nezapisoval.
