# Fáze D8–D12 – návrh

Status: Michal 19. 11. „dodělejte celou fázi D“. Navazuje na roadmapu (D8–D12 a rozšíření o reverzní inženýrství). Platí zde i bezpečnostní podmínky z roadmapy: self-audit neznamená, že se agent schválí sám.

## 1. Zásada: jen čtení, jen zpráva

Každá část D8–D11 je **místní CLI příkaz, který jen čte**. Nepřidává metodu brokeru, nepíše do DB, do konfigurace klientů, do hooků ani do repozitáře. Výstupem je Markdown (nebo JSON přes `--json`) na stdout, případně do souboru `--out`.

- Data z místnosti se čtou ze **snímku DB** (SQLite backup API do dočasného adresáře 0700). Ostrá DB se tak nikdy neotevře pro zápis ani migraci.
- Výstup prochází `redact`, stejně jako zprávy v místnosti. Hlavní ochrana ale je, že se citlivé hodnoty do zprávy vůbec nedostanou (#31): důkaz obsahuje jen název programu, cestu ke konfiguraci a událost, nikdy celé argv, env ani text chyby parseru (jen typ a řádek). U `last_error` se ukáže jen bezpečná kategorie (předpona typu `research:`), ne surový text.
- Návrhy optimalizací jsou jen text. Provede je agent běžnou cestou (úkol → review → člověk).

## 2. D8 – `agent-lease self-audit`

Doplňuje `doctor`. Doctor odpovídá „funguje koordinace?“, self-audit odpovídá „co je v harnessu rozbité nebo zbytečné a proč“.

| Oblast | Kontrola | Důkaz ve zprávě |
| --- | --- | --- |
| Konfigurace | `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.codex/config.toml` jdou načíst | soubor a chyba parseru |
| Hooky | příkaz každého hooku existuje (absolutní cesta, nebo nalezený v PATH) | událost, příkaz |
| MCP | servery z `~/.claude.json` a `config.toml`: příkaz existuje | název serveru, příkaz |
| Skills | `~/.claude/skills/*/SKILL.md` má frontmatter `name` a `description`, názvy se neopakují | cesta |
| Nástroje | `claude`, `codex`, `git`, `bwrap` v PATH | nalezená cesta |
| Chyby | `needs_review` a selhání podle důvodu (`last_error`), za posledních N dní | počet a příklady ID |
| Zbytečná volání | úkoly s ≥ 3 pokusy, pokusy bez reportu spotřeby, nepřečtené zprávy, které vypršely | počet a ID |

Každý nález má závažnost, příčinu, důkaz a návrh. **Doctor se z auditu nevolá** (#31): bez brokeru by přes `open_store` otevřel Store nad ostrou DB a zapisoval do ní. Spouští se zvlášť. Test: izolovaný HOME se záměrně rozbitou konfigurací (nevalidní JSON, hook na neexistující program, skill bez frontmatteru, duplicitní skill) a DB s opakovanými pokusy. Audit najde všechny chyby a zdroje nezmění (porovná se hash před a po). Druhý test spouští audit bez brokeru přes CLI, se syntetickými tajemstvími v konfiguraci i v `last_error`. Ta se nesmí objevit v Markdownu ani v JSON a DB zůstane bajtově stejná.

## 3. D9 – `agent-lease recall-bench CASES.toml`

Sada kontrolních dotazů nad D1 (`knowledge`):

```toml
[[case]]
query = "bridge nespustí codex po aktualizaci"
project = "agent-lease-mcp"
expect = [12]          # ID záznamů, které mají přijít
forbid = [7]           # zastaralé/zamítnuté, nesmí přijít jako odpověď
k = 5
```

Hledá se **přesně v režimu, který dostane agent** (`knowledge_search`: ověřené první, neověřené ano a označené, vyřazené ne). Relevantní znamená ID v `expect`. Případ, jehož očekávaný záznam už neexistuje, je `stale`/`rejected` nebo patří jinému projektu, se vypíše jako **zastaralý případ** a do metrik se nepočítá. Metriky: precision@k, recall@k a MRR na dotaz i celkem. Dále se počítá **chybné vybavení** (vrácený záznam z `forbid` v běžném režimu) a **neověřené ve výsledku**. Vyřazené záznamy se měří odděleně jako **diagnostika** (druhé hledání s `include_retired`), do metrik se nepočítají. `--baseline předchozí.json` ukáže rozdíl, ale jen při stejném otisku sady (dotazy, projekt, expect, forbid, k). Jinak skončí chybou. Běží nad snímkem DB, nebo nad zadanou testovací DB (`--db`). V repu je vzorová sada s fixturou a test na známé hodnoty metrik.

## 4. D10 – `agent-lease efficiency`

Kohorta: úkoly **odeslané** v období (`--days`). Atribuce: každý pokus se počítá příjemci, který ho spustil (`task_usage.recipient`). Při failoveru má úkol doručenku u každého příjemce zvlášť, takže se objeví v obou skupinách. Skupiny jsou příjemce × model (skutečný, a požadovaný, když se liší) × druh zprávy. Pro každou skupinu: úkoly, **dokončené** (stav doručení `succeeded`, **ne ověřená správnost**), pokusy na úkol, tokeny a cena na dokončení a medián doby. Cena je jen popisná. „Cena za správnost“ by vyžadovala párované ověřené úlohy, ty zatím nemáme, a proto se nevykazuje. Neznámé tokeny, neznámá cena a pokusy bez reportu se vedou zvlášť. Skupina s neznámou hodnotou nemá průměr.

Nic nedoporučuje automaticky. Zpráva výslovně píše, že malý vzorek (< 5 dokončených) se neporovnává, a nevyvozuje žádnou „univerzální“ hodnotu. Volbu modelu a úsilí dělá člověk nebo zadavatel.

## 5. D11 – `agent-lease health [cesta]`

Statická analýza Pythonu přes `ast`, bez nové závislosti:

- **duplicitní implementace**: funkce se stejným normalizovaným tělem (bez názvů a docstringů), aspoň 5 příkazů;
- **kandidáti na mrtvý kód** (heuristika, ne důkaz): funkce a třídy na úrovni modulu, na jejichž název nikde jinde neukazuje žádný identifikátor ani řetězec. Výjimky: `__all__`, dunder metody, entrypointy z `pyproject.toml` a testy;
- **cykly importů** při načtení modulu (líný import uvnitř funkce cyklus záměrně láme, nepočítá se);
- **nekonzistentní názvy**: funkce mimo snake_case. Metody se nehodnotí, protože jejich názvy jako `do_GET` diktuje rodičovská třída.

Výstupem jsou jen nálezy. Pilot na tomto repu vyhodnotí falešné poplachy ručně (dokument v `docs/research/`). GitNexus se neinstaluje. Volitelný pilot jen po souhlasu.

## 6. D12 – zásady práce (Karpathy)

1. Inventura jen čtením podle roadmapy: **čtyři zásady** (Nejdřív porozumět, Nejjednodušší dostačující, Jen relevantní změny, Doložený výsledek) proti `CLAUDE.md`/`AGENTS.md`, skills, **hookům** a pravidlům agent-lease. Ada: Claude Code (`docs/research/2026-11-19-d12-inventory-ada.md`). Nox: Codex. Bez soukromých instrukcí v commitu.
2. Návrh minimálního textu (≤ 15 řádků) jako diff. **Do instrukčních souborů se nic nezapíše bez výslovného Michalova souhlasu.**
3. Pilot podle roadmapy (baseline proti pravidlům) až po souhlasu. Stojí tokeny obou agentů.

## 7. Rozšíření D3–D6 o reverzní inženýrství

Akceptační test podle roadmapy: neškodná testovací binárka zkompilovaná z malého zdrojového souboru v repu, statická analýza dostupným nástrojem (Ghidra/REA MCP, jen čtení). Ve zprávě budou verze nástroje, hash artefaktu, funkce, důkazy a nejistoty. Nox nezávisle zreviduje. Zápis nebo flash firmwaru se neprovádí. Bez souhlasu a zálohy je blokovaný pravidlem. Kódem se nevynucuje, protože agent-lease takový nástroj nespouští.

## 8. Co se nedělá

Žádná automatická oprava nálezů, žádný plánovač auditu (spuštění z cronu nebo Hermes je volba operátora) a žádná nová závislost. D10 nevybírá model automaticky.

## 9. Akceptační testy

- D8: rozbitý izolovaný HOME → všech 5 typů nálezů s příčinou; soubory beze změny; ostrá DB se neotevře pro zápis.
- D9: fixtura se známými ID → přesné hodnoty P@k, R@k, MRR; vrácený `forbid` a neověřený nad ověřeným jsou vidět; porovnání s baseline.
- D10: fixtura dvou modelů → cena na dokončený úkol; nehlášené pokusy zvlášť; malý vzorek označen.
- D11: fixtura s duplikátem, mrtvou funkcí, cyklem a camelCase → přesně tyto nálezy; použité funkce a entrypointy nejsou mrtvé.
