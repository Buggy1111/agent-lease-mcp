# Nezávislá kontrola Agent Lease, 19. 11. 2026

Úkol #1647 od claude-code, kontroloval Nox. Žádné nasazení, instalace, push, změna konfigurace ani zápis provozních dat. Pouze accept/send/claim jako koordinace a doctor s dočasnou zkouškou zápisu; DB čtena přes SQLite mode=ro.

**Verdikt: HOTOVO S ZNÁMÝMI OMEZENÍMI pro současnou běžnou koordinaci. Celý plán A–D dokončený není.** Kontrola neodhalila novou poruchu běžícího jádra. Ostrá aktivace dalších funkcí a některé akceptační zkoušky zbývají; podrobnosti níže.

## Provoz

- `current` míří na `runtime-0175752`. Všech 54 Python modulů instalovaného balíčku je bajtově shodných s čistým exportem `origin/main` = `HEAD` = `5e8a171caab67fdcf30845fd7cdd093a4f6bf2aa`. Rozdíl `0175752..5e8a171` je pouze 16 řádků deploy dokumentace. V pracovním repozitáři existuje nesledované `.claude/`; do testovacího exportu nevstoupilo. Vzdálený server GitHub se nekontaktoval; origin/main je lokální ref.
- Broker, oba bridge a codex-wake jsou active/running, NRestarts=0, spuštěné 20:25:45–46 CET. `/proc/PID/exe` všech čtyř míří do runtime-0175752. Oba bridge mají Delegate=yes a argument --cgroup.
- Oba nalezené interaktivní procesy agent-lease-mcp (PID 1667939 a 1671757) rovněž používají runtime-0175752.
- Doctor z absolutní cesty current, AGENT_NAME=codex: exit 0, Looks good. SQLite 3.53.1, identity/hooky/broker/wake v pořádku, provider available via codex_app_server. Varování WSL lifetime, Jev off (volitelný), 6 přijatých tasků bez startu.
- Provozní room.db: integrity_check=ok, journal_mode=delete. Kontrola provedena read-only.
- Webový proces ani listener :8765 neběží. Historická transient služba webui-deploy je inactive. README výslovně nabízí spuštění `agent-lease web --open` na vyžádání; Ada potvrzuje, že trvalá jednotka neexistuje. Není to pád čtyř základních služeb. Trvalý samostart webu není hotový.

## Fronta

Pět položek claude-code needs_review je starých: #594, #603, #605, #617 (5. 11., provider limit) a #623 (6. 11., expirace lease po startu). Jde o bezpečně zadržené pokusy, nikoli nové pády aktuálního runtime. Nelze je bez prověření označit za neškodné a automaticky zopakovat: účinky přerušeného běhu mohou existovat. #623 má pozdější dokončovací report PASS #637; run-fence oprava z #603/#605 má pozdější nezávislé PASS v PHASE_A_RUN_FENCE_REVIEW_2026-11-05.md. Fronta tedy není uklizená podle pozdějších výsledků.

Šest pending tasků pro Codex zahrnuje historické #573/#577/#581/#586 (5. 11.), #1223 (15. 11.) a právě prováděný #1647. Doctor ukazuje přijaté bez startu: interaktivní accept je příjem a není dokončení tasku. Žádná z těchto položek nebyla v tomto auditu zrušena nebo přepnuta.

## Co skutečně zbývá z A–D

- A5/A10: služby již mají cgroup režim a dohled; stará tabulka a text „nenasazeno“ nejsou aktuální. Stále nelze dát absolutní záruku zastavení libovolného callable runneru či procesu, který má stejná uživatelská oprávnění a může ochrany obejít. Bez cgroup zůstává známý xfail odděleného potomka. Externí účinky vyžadují idempotenci cíle.
- A8: opravená SQLite je skutečně v živém runtime (3.53.1), journal DELETE, integrita ok. Staré označení „ověřování“ už nevystihuje provoz.
- A9: heuristika karantény není úplná bezpečnostní hranice. Stejný OS uživatel může obejít API přímým přístupem k souborům/DB; oddělení uživatelů není doloženo jako vyřešené. Nejde o novou závadu tohoto nasazení.
- A11: PHASE_A_A11_RESULT_2026-11-06.md dokládá skutečný restart WSL a samostart služeb PASS. Plný restart Windows a automatické spuštění WSL po přihlášení ověřeny nejsou; web nemá trvalý samostart. Není přesné říkat, že celý A11 je neprovedený.
- B: PHASE_B_OWNER_APPROVAL_2026-11-11.md výslovně zaznamenává Michalovo uzavření B a přechod do C. Tvrzení roadmapy/TODO „B zůstává otevřená, C nezačíná“ je zastaralé. Toto schválení neaktivovalo B7. `quality-runner.toml` chybí, ostrá automatická brána není zapnutá; její akceptace/enforcement napříč projekty zůstává samostatnou prací. V auditu se neověřovala živá GitHub branch protection.
- C: implementace i nasazení jsou doloženy novějšími deploy záznamy, starý návrh nepopisuje současný stav. Autonomní režim má explicitní MODE_LOCKS a `fix-runner.toml` neexistuje. Ostré automatické opravy/pilot proto nejsou zprovozněné. Vyžadují vlastníka, politiku a akceptaci; zamčení je úmyslné.
- D1/D2 a D8–D11: implementované a nasazené; režim místnosti intelligent. D3–D7 kód je nasazen, ale `research.toml` a potvrzení `research-probe.json` chybí, takže ostrý výzkumný tým je záměrně vypnutý.
- D12/D13/katalog: inventury a piloty mají review, ale plné baseline/A-B měření přínosu obou agentů neproběhlo. D13 report přiznává zbývající vizuální vady a neprohlašuje render za plnou akceptaci. Katalog je dokument, nikoli automatické sdílení nástrojů mezi profily. Toto není porucha koordinace.

## Ověření kódu

Čistý git archive main v tomto adresáři, bez editací produkčního repozitáře. Ruff src/tests PASS. První pytest běh v omezeném sandboxu přerušen: 121 passed, chyby PermissionError při bind lokálních socketů; tento běh není důkaz regrese aplikace.

Opakovaný kompletní běh s izolovaným HOME, syntetickou DB, PATH bez skutečných klientů agentů a existujícím vývojovým venv: **948 passed, 30 skipped, 1 xfailed, 37 errors**, 391,18 s (`pytest-unrestricted.log`). Všech 37 setup errors je z `tests/test_fix_runner.py`: příliš dlouhá cesta syntetického HOME → AF_UNIX path too long (fixture ř. 49). Cílená reprodukce s `-x` potvrdila právě tuto příčinu. Celý dotčený soubor byl opakován s krátkým syntetickým HOME `/home/buggy1111/dev/n` mimo /tmp: **38 passed**, 40,74 s (`pytest-fix-short.log`). Jeden test se překrývá s původními 948 PASS, tedy souhrnně **985 unikátních testů PASS, 30 SKIP, 1 očekávaný XFAIL**. Nešlo o jediný zelený kompletní běh a nesmí se tak vykazovat. Nezůstalo nevysvětlené selhání.

30 SKIP je opt-in izolované B7 E2E, které tento audit nezapínal. XFAIL je známý oddělený potomek bez cgroup; varování je deprecace authlib.jose. Izolované testy a logy byly ponechány, žádná hromadná mazání neproběhla.

Neopakoval se osmihodinový chaos, restart Windows/WSL, skuteční poskytovatelé ani zápisový pilot. Kontrola procesů dokládá runtime, nikoli správnost všech budoucích běhů.
