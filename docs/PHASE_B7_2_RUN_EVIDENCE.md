# B7.2a — evidence běhů a adaptér dashboardu

Stav: místní implementace, autor Nox, nezávislé review pending. Nenasazeno.
Navazuje na [B7.1](PHASE_B7_1_INSPECTION.md) a [REA mapu existujícího dashboardu](research/2026-11-07-b7-rea-dashboard-reuse.md).

## Co je implementované

Stávající `Store` má novou tabulku `quality_runs`, interní kontrakt
`CommandObservation` a privátní `_quality_record_run`. Nezakládá se druhá DB,
server, transport, scheduler ani alternativní schvalování.

Zapisovací metoda je určena budoucímu izolovanému runneru v procesu důvěryhodné
služby. Přijímá jeho procesní pozorování, nikoli předložený JSON report se stavy.
Kontroly tests/lint/typecheck/build dostanou stav podle ukončení procesu;
timeout nebo nenulový návrat povinné kontroly je FAIL. Úspěšný příkaz coverage
má REVIEW, dokud adaptér skutečně neověří číselnou metriku. Chybějící pozorování
zůstává NOT RUN; nepodporovaná metrika N/A. Samotné inspection nelze uložit jako
běh a prázdná sada pozorování se odmítne.

Před zápisem se znovu ověří deklarace přesných Git objektů z B7.1. Odmítnou se
změněný plán, opt-out, chybná konfigurace, duplicitní pozorování i pozorování
nedeklarovaného příkazu. Plný commit, comparison base a config digest jsou
součástí evidence. Větev není odhadována a zůstává null; vazba na task ještě není
implementovaná. Čas je čas zaznamenání na serveru, nikoli předstíraný začátek
měření. Identita execution je idempotentní: stejný obsah vrátí stejný run,
konfliktní opakování se odmítne. Zápis a audit jsou v jedné transakci.

## Hranice autority

Broker dovoluje pouze `quality_runs` a `quality_get`. Privátní zapisovací metoda
není v allowlistu, není veřejné MCP/HTTP/CLI odeslání vlastního výsledku. Projekt
nemůže poslat vlastní `overall_status`, security verdict ani reviewer approval.

Toto oddělení **není důkaz provedení libovolně předloženého exit code**.
Zapisovací metoda sama proces nespouští a lokální Python kód stejného uživatele
není před SQLite chráněný. Platí existující model důvěry brokeru na úrovni OS
uživatele. Až navazující runner nasbírá pozorování sám, může volat tento privátní
sink. Nesmí se přidat obecné broker RPC, které by klientský report pouze předalo
zapisovací metodě. V této etapě zapisují běhy jen testy do syntetických DB.

Security a nezávislé schválení se při čtení odvozují ze stávajícího B6 review:
shodný repozitář, head **i base**, platný verdict pro celý SHA, odlišný recenzent
od autora i vykonavatele, `security_by` shodný s recenzentem, scan stejného
commitu a prázdné findings. Nejnovější review pro tento diff má přednost, proto
nové čekající review nepřebere staré schválení. Platné uzavřené approved review
se zachovává. Změna stavu review se projeví i u již uloženého běhu; PASS se
neukládá jako trvalá autorita. Explicitní security fail je FAIL; chybějící nebo
neplatné review zůstává pending/REVIEW. Policy paths z B7.1 se peer schválením
nepromíjejí: lidská výjimka ještě není implementovaná a zůstávají REVIEW.

Evidence neobsahuje raw stdout/stderr, argumenty, skripty ani volně dodaný text
runneru. Má pouze počet bajtů, SHA-256 výstupu, exit code, timeout a trvání.
Nejde o plnohodnotné protokoly: odkazy na redigované logy zatím nejsou hotové.

## Čtení a stávající dashboard

Seznam vrací poslední běh pro každý kanonický repozitář, nejvýše 100 položek,
dashboard 30. Pět předchozích běhů téhož repo tvoří omezenou historii. Stavy
historie jsou také současné projekce B6 autority; nejde o neměnný historický
snímek schválení. Repozitáře stejného basename se nespojují. Čtení neprovádí Git
ani projektové příkazy a kontroluje review ve stejné SQLite read transakci.

Autorizovaný `/api/snapshot` a stávající SSE room event poskytují shodné pole
`quality_runs`. Reuse odpovídá REA mapě: `renderRoom` → původní QG renderery,
žádný další fetch/polling. Prohlížeč připouští pouze `kind=quality_run` a
`executed=true`, inspection ignoruje. Demo používá oddělená data a po vypnutí
obnoví poslední skutečný snímek. Výběr detailu je vázaný na `run_id`, takže
přidaný řádek nezobrazí detail jiného repo. Prázdný seznam známý od serveru má
nulu; chybějící pole staršího serveru je neznámý stav. Všechny popisky z dat
nadále procházejí existujícím escapováním.

## Ověření

Výsledky a přesné prostředí: [validation.json](test-results/b7-2a/validation.json).
Testy používají pouze lokální syntetická repa/DB a mockovaný HTTP/SSE server.
Procesní pozorování v testech jsou záměrně syntetická; nejsou prezentována jako
E2E skutečného runneru. Pokryty jsou nepovolené RPC přes skutečný Unix socket,
restart/persistence, idempotence, neplatné/stale/self review, timeout/nonzero,
policy changes, TypeScript typecheck, chybějící a neověřená coverage, HTTP auth
a shoda SSE projekce. Prohlížeč ověřuje živý QG, demo, stabilní detail, XSS,
responsivitu a stávající ovládání místnosti.

## Co zbývá v B7.2

- Izolovaný runner nad přesným snapshotem, ověření nástrojů a schválené prostředí,
  zdroj pozorování výhradně z vlastního supervisoru. Nepřebírat výsledky projektu
  nebo agenta jako autoritu. Zachovat stop/pause a limity i pro potomky.
- Vazba běhu na oprávněný task, bezpečné redigované logy, ověřené metriky.
- `agent-lease quality check`, Python/TS skutečné E2E s PASS/FAIL/REVIEW a scénáři
  obou agentů. Tato etapa není jejich splněním.
- Nezávislé review, případná explicitní lidská policy výjimka a až zvlášť
  schválená aktivace hooků / nasazení. B7 ani fáze B nejsou označené za hotové.

Produkce zůstává na 9134485. Žádné změny agentích konfigurací, žádný push/merge,
žádná instalace závislostí a žádné zprávy Adě.
