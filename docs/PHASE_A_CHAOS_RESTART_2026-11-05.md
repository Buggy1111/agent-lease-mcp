# Restart izolovaného chaos ověření — 5. 11. 2026

Autor: Nox / Codex. Autorizace: Michal požádal „mrkni na to ju at ty testy jedou“; Ada má podle Michala kapacitu do 19:40. Produkční služby, místnost, konfigurace agentů ani účty nebyly změněny. Níže uvedené systemd jednotky jsou pouze nové dočasné izolované testy.

## Zjištěný stav

- `al-chaos-8h-2.service`: běh 4. 11. 23:04:23 až 5. 11. 07:35:59, exit 1. Log `~/.local/share/al-8h-runtime/chaos8h-2.log`: 50 pending po dojezdu. Bez dvojitých efektů, integrita DB v pořádku. Původní problém opravovala Ada v `bd382b4` a `17ef19b`.
- `al-chaos-15b.service`: běh 5. 11. 15:14:52 až 15:35:21, exit 1. Log `~/.local/share/al-8h-runtime/chaos15-17ef19b.log`: 2 pending. Izolovaná DB `/tmp/al15b/agent-lease-chaos-7iwvcc5b/room.db` ale ukazuje úspěšný postup až do konce dojezdu: provider recovery v 15:34:57, několik Done a poslední accept #701 v 15:35:10. Není to důkaz dalšího zablokování pojistky; pevný dojezd 300 s skončil během aktivní práce po simulovaných minutových limitech.
- Pro nový test je dojezd omezen na 900 s. Nejde o změnu akceptačních podmínek: nevysvětlené pending, dvojité efekty, falešné Done, sirotci či porušení integrity stále znamenají neúspěch.

## Dodatečně reprodukovaná chyba kandidáta 17ef19b

Automatická obnova prázdného markeru staršího než 30 s předpokládala, že tvůrce markeru již nežije. Živý proces ale může být pozastaven mezi `open(O_EXCL)` a `flock`. Jiný proces pak mohl starý marker odstranit a další pokus vytvořit nový inode; původní tvůrce by po obnovení zamkl odpojený starý inode. Samotné stáří markeru tedy nestačí k bezpečnému rozhodnutí.

Nový test `test_recovery_cannot_remove_marker_while_live_owner_is_before_flock` před opravou skutečně selhal (`recovered=True`). Oprava serializuje vytvoření a zamčení markeru s obnovou přes zámek stabilního rodičovského adresáře. Obnova jej získává neblokujícím způsobem; aktivního tvůrce neobejde. Kontrola zámku markeru, inode a cgroup zůstává zachována. Žádná neověřená práce se automaticky neopakuje.

Druhý nový test nechá skutečný podproces skončit mezi vytvořením a zamčením markeru a ověřuje, že po jeho smrti lze marker obnovit a spustit další pokus.

## Ověření a přesná identita nového kandidáta

- Původní úplná sada na `17ef19b`: **497 passed, 1 xfailed**, exit 0; chybějící regresi nekryla.
- Opravený `tests/test_run_fence.py`: **13 passed**. Ruff nad dvěma změněnými soubory a `git diff --check`: úspěch.
- Úplná sada opraveného snímku: **499 passed, 1 xfailed**, exit 0, 165,29 s. Jedno stávající upozornění na zastarávající `authlib.jose`. Výsledek zaznamenán v `unit-result.json` v adresáři níže.
- Nezávislé review opravy vyžádáno od Ady přes agent-lease #603 a #605. Bridge převzal #605, ale výsledek #615 skončil hláškou „You've hit your 5-hour limit · resets 7:40pm (Europe/Prague)“. Review tedy provedeno nebylo; nejbližší hlášený reset je v 19:40. Vlastní kontrola Noxe není označena jako nezávislé review. Izolované testování pokračuje, nasazení tím není schváleno.

Nový adresář: `/home/buggy1111/dev/work/agent-lease-chaos-20261105-fixed/`.

- Základ: `17ef19b9e94d2c9e241459da9b2e508c3267c9ae`.
- Změněné soubory: `src/agent_lease_mcp/run_fence.py`, `tests/test_run_fence.py`.
- Přesný patch: `candidate.patch`, SHA-256 `a5be59a857c0dfa9989f962e2b6cdbd5f6a8922eef21d3f8aeb37ae4950ca370`.
- Zmrazené zdroje: `snapshot/`; otisky 76 zdrojových/testovacích souborů v `source-manifest.json`. Orchestrátor je ověřuje před každou fází.
- Runtime: CPython **3.13.18**, SQLite **3.53.1**, existující `~/.local/share/al-8h-runtime/venv/bin/python`.
- Jednotka: `al-chaos-fixed-20261105.service`, `Delegate=yes`, `KillMode=control-group`, nový testovací cgroup. Čisté prostředí bez zděděných údajů účtů; syntetický agent, oddělené DB, skutečné provider probes zakázány stávajícím harnessem.
- Krátká fáze: **15 min**, 4 workery, `--cgroup`, maximálně 900 s dojezdu. Start **15:59:24 CET**.
- Dlouhá fáze: **480 min**, stejné parametry; spustí se pouze při exit 0 a PROŠLO krátkého chaosu, exit 0 regresní sady a shodném manifestu zdrojů. Při chybě se další fáze nespouští; žádný automatický restart.
- Živý stav: `state.json`; průběh: `short.log`, později `long.log`; koordinace: `validation.log`; výsledky a kontrolní součty logů: `short-result.json`, později `long-result.json`.

Předchozí pomocný běh `al-chaos-nox-20261105.service` na původním kandidátu byl po reprodukci chyby záměrně zastaven. Jeho adresář `../agent-lease-chaos-20261105-nox/` je zachován a stav označen jako stopped, nikoli jako úspěšný či neúspěšně dokončený chaos.

## Stav uzavření

Testy jsou spuštěné. Osmihodinový úspěch zatím doložen není a fáze A ani nasazení nejsou schválené. Po skončení číst skutečný výsledek `long-result.json` a celý log; při úpravě runtime kódu je nutné ověření nového přesného snímku. Provozní nasazení nadále vyžaduje Michalův výslovný souhlas.

Kontrola 16:05:56 CET: krátká fáze stále běží; poslední průběhový záznam uvádí 130 odeslaných úkolů, 31 pádů workerů a 12 pádů brokeru. Regresní brána je již splněná a manifest snímku odpovídá. Dlouhá fáze začne bez dalšího ručního zásahu pouze po úspěšném krátkém chaosu; při selhání se řetězec zastaví a zachová důkazy.

## Výsledek 8h běhu (doplnila Ada, 6. 11. 2026 00:45, úkol #623)

- `al-chaos-fixed-20261105.service`: Result=success, ExecMainStatus=0; `state.json` stage `complete`, status `passed`.
- Běh 5. 11. 16:18:09 → 6. 11. 00:36:04, `long-result.json` exit 0, log SHA-256 `1a0882ea18125761176f5499b2d11274199ac08b8f631b62c38a793c09e82805`.
- 7804 úkolů; 3929 pádů workerů, 980 pádů brokeru, 574 zrušení, 1665 interaktivních (845 pádů), 1 pauza, 274 simulovaných limitů (zotavení pozorováno).
- Stavy: succeeded 3754, needs_review 3476, cancelled 574, **pending 0**.
- Invarianty: žádné dvojité efekty, succeeded právě s 1 efektem a 1 Done, žádné Done u nedokončeného, karanténa nikdy spuštěna, žádný start ani efekt během pauzy, žádní přeživší ani sirotci po pádu bridge, nic nevisí; integrita DB ok; pokrytí scénářů ano.
- Identita: `src/` snímku je shodný s pracovní kopií (`diff -r`), `git diff -- src tests` má SHA-256 patche `a5be59a8…`. Commit níže obsahuje přesně tento kód.
- Nezávislé review patche: Ada PASS (#627, `PHASE_A_RUN_FENCE_REVIEW_2026-11-05.md`).
- Evidence: `docs/test-results/phase-a-run-fence-fixed/`.

**Verdikt: PASS.** Provozní nasazení zůstává podmíněné výslovným souhlasem Michala (plán `PHASE_A_DEPLOY_PLAN.md`), poté A11.
