# ADR 0002 — SQLite na disku a stdio, ne dlouhoběžící server

**Stav:** přijato, 10. 9. 2026

## Kontext

Stav místnosti (nájmy, přítomnost, vzkazy, audit) čtou a zapisují **tři různé
procesy**: MCP server spuštěný Claude Codem, MCP server spuštěný Codexem a hook,
který běží samostatně při každém volání nástroje. Nic z toho nesdílí paměť.

## Varianty

**A. Stav v paměti serveru.** Nefunguje. Každý MCP klient si přes stdio spouští
vlastní proces — dva servery by měly dva nezávislé pohledy na svět a hook by
neměl přístup k žádnému.

**B. Jeden dlouhoběžící HTTP server**, oba klienti jako HTTP MCP klienti.
Umožnil by push notifikace a živý chat. Cena: proces navíc, který musí někdo
spouštět a hlídat, port, a hlavně další věc, co může být rozbitá ve chvíli, kdy
potřebuješ pracovat.

**C. SQLite na disku, stdio servery.** Sdílené je jen jedno: soubor.

## Rozhodnutí

**C.** WAL režim, `busy_timeout`, `BEGIN IMMEDIATE` u nájmu.

Souběh je tu skutečný a je to celý smysl projektu, takže se nedá odbýt:
`BEGIN IMMEDIATE` bere zámek na zápis hned na začátku transakce. Bez něj dva
agenti oba přečtou „volno" a oba si cestu vezmou — přesně ta chyba, kterou má
tenhle projekt hlídat. Je na to test se dvěma vlákny a jedním vítězem.

## Důsledky

- **Žádné push notifikace.** MCP je pull: vzkaz si protistrana přečte, až se
  sama podívá. Pro nájmy a předávání to stačí, na živý chat to bude působit
  zpožděně — a je to napsané v README, ať to nikoho nepřekvapí.
- Funguje to jen **na jednom stroji**. Pro dva agenty na jednom notebooku je to
  přesně to zadání.
- Databáze je čitelná běžným `sqlite3` — po kolizi se dá do stavu podívat i bez
  agenta.
- Žádná abstraktní vrstva úložiště. Druhé úložiště v dohledu není a `Store` je
  malý; až přijde potřeba (víc strojů), je to jedna třída k přepsání.

## Kdy to přehodnotit

Až budou agenti na různých strojích, nebo až bude push notifikace opravdu
potřeba. Pak HTTP transport a stav v Postgresu — ale to je jiný produkt, ne
evoluce tohohle.
