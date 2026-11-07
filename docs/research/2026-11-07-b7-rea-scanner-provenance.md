# B7 — cílená statická RE kontrola scannerů, 7. 11. 2026

Michal při pokračování B7 navrhl využít reverzní inženýrství. Použita již dostupná REA `inspect_go_binary` na dvou veřejně vydaných MIT artefaktech stažených a ověřených při [B7.0 pilotu](2026-11-07-b7-0-pilot.md). Binárky se v této analýze nespouštěly; žádný disassembler, dynamická instrumentace, síť, instalace ani analýza zařízení nebyly potřeba. Je to úzká kontrola distribuovaných artefaktů v B7, nikoli zahájení fáze D nebo nový orchestrátor.

| Pozorování ze souboru | Gitleaks 8.30.1 | Betterleaks 1.10.0 |
| --- | --- | --- |
| Formát / CPU | ELF amd64 | ELF amd64 |
| Velikost binárky | 21 958 840 B | 25 133 216 B |
| Vložená verze Go | go1.24.11 | go1.27.3 |
| Počet dekódovaných dependency záznamů | 66 | 54 |
| Vložený main module | chybí | chybí |
| Module path | `command-line-arguments` | `command-line-arguments` |
| Linker flags | `-s -w`, version symbol 8.30.1 | `-s -w`, version symbol 1.10.0 |
| Deklarované build settings | CGO_ENABLED=0, GOOS=linux, GOARCH=amd64 | CGO_ENABLED=0, GOOS=linux, GOARCH=amd64 |

U Gitleaks je navíc zaznamenán build tag `gore2regex`. Tyto položky jsou **pozorované vložené údaje**, nikoli potvrzení skutečného kompilátoru nebo autentické provenience. Dekódování tabulky je kompletní, ale bez main-module záznamu zůstává úplnost runtime závislostí neznámá. Menší seznam u Betterleaks nedokazuje nižší riziko nebo lepší detekci.

Hash ELF z REA byl nezávisle porovnán s přesnými bajty členu původního tar archivu; SHA-256 archivu znovu odpovídá již ověřenému release manifestu. [Řetězec identity](b7-rea/artifact-chain.json), [úplný REA důkaz Gitleaks](b7-rea/gitleaks-go-evidence.json), [úplný REA důkaz Betterleaks](b7-rea/betterleaks-go-evidence.json). Obě verze ve vložených linker údajích souhlasí s explicitními verzemi z předchozího pilotu.

## Praktický přínos a omezení

Kontrola přidává identitu skutečných spouštěných ELF souborů a reprodukovatelný inventář deklarovaných závislostí i při `-s -w`. Nemusíme kvůli tomu instalovat Go či Ghidru nebo binárku spouštět. Je to vhodný doplněk kontroly dodavatelského artefaktu před volitelným runnerem B7; nenahrazuje výběr pinnutého zdroje a checksumy.

Nebyl obnoven zdrojový kód, funkce, call graph, typy, síťové endpointy ani validační cesty. Tato statická metadata **neprokazují**, že binárka nekomunikuje po síti, nemění soubory či nevaliduje klíče. To nesmí oslabit offline sandbox, explicitní důvěryhodný config, redakci 100 % a `validation=false`, ověřené samostatným pilotem. Vulnerability audit závislostí ani ověření podpisů nejsou tímto výsledkem tvrzeny.

Rozhodnutí: **POUŽÍT jako čtecí provenienční důkaz**, nikoli jako bezpečnostní PASS nebo důvod zařadit nový scanner do runtime. Původní rozhodnutí pilotu (ponechat B6, nové scannery zatím nepřidávat jako povinnou bránu) zůstává. Autor této kontroly je Nox; nezávislé review nadále pending, Adě se nic neposílalo.
