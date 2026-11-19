# B7.0 — nezávislé review Noxe

Auditovaný commit: `8774d5e11f86f0b41e19ba8e684e13a209eaf93c`, review #7, 7. 11. 2026.
Verdikt: **CHANGES REQUESTED**. Jde o dokumentační audit a návrh, nikoli implementaci. Pilot jsem nespustil, žádnou závislost neinstaloval a žádnou provozní konfiguraci nezměnil. Níže uvedené podmínky nejsou schválením produkční integrace.

## Zjištění vyžadující opravu

### P1 — cgroup/venv/worktree nejsou izolace nedůvěryhodného kódu

Audit ř. 59–71 a 77 navrhuje příkazy z CI/package.json a „stejnou izolaci jako bridge“. Na přesném auditovaném commitu `bridge._run` spouští `process_guard` bez očištěného `env` a bez filesystem/network namespace; guard spouští předané argv jako běžný podproces. Cgroup, timeout, run-fence a PDEATHSIG řeší životní cyklus, nikoli přístup ke stejnému uživatelskému účtu. Ochrany výchozího `codex exec --sandbox read-only` se nepřenášejí na libovolný pytest/npm příkaz. Testy, build backendy, conftest a instalační skripty umějí spustit kód; venv ani snapshot nechrání domovský adresář, jiné repozitáře, broker socket nebo síť.

Oprava: oddělit detekci příkazů od důvěry a jejich spuštění. Pro cizí repo požadovat skutečně vynucené oddělení filesystemu, sítě a oprávnění: žádný host HOME ani broker/SSH/agent sockety, čisté allowlist prostředí, zapisovatelný jen scratch/output, bez eskalace oprávnění, omezení CPU/paměti/procesů/času a velikosti logů. Není-li takové prostředí dostupné a ověřené, vrátit SETUP REQUIRED/REVIEW a nic nespouštět. „Bez sítě kde jde“ nahradit fail-closed podmínkou. Bez schválení neměnit host sandbox, služby ani oprávnění. `npx` může stáhnout chybějící balíček; používat ověřené již připravené projektové binárky bez automatického fetch/install.

Malý pilot lze navrhnout zvlášť pouze pro náš konkrétní známý commit a explicitně zkontrolované příkazy, se zachováním existujícího sandboxu. Tím se nesmí deklarovat podpora spouštění libovolných repozitářů. Ani tuto alternativu tento review sám nespouští.

### P1 — konkretizovat bezpečný scanner pilot a ochranu nálezů

Betterleaks v1.10.0 má online validaci, volitelné načítání konfigurace z cíle a odlišné výchozí limity dekódování/archivů; není bezpečné převzít příkaz z README nebo tvrdit neověřenou úplnou drop-in kompatibilitu. `cmd/root.go` na `8d77e1712fb06fd14444cc648686f1d5655e28a8` má `validation=false`, `unsafe-auto-load-config=false`, ale výchozí `redact=0`. Před pilotem stanovit explicitní offline lokální režim, vypnutou validaci a diagnostický HTTP server, kontrolované prostředí/config, zákaz target-selected konfigurace, úplnou redakci a ověření redakce ve všech výstupech, nejen stdout. Žádné skutečné hodnoty nálezů do místnosti, reportu či commitu. [Ověřený zdroj příznaků](https://github.com/betterleaks/betterleaks/blob/8d77e1712fb06fd14444cc648686f1d5655e28a8/cmd/root.go).

Oba scannery porovnat nejprve nad shodným syntetickým korpusem se známými nálezy i negativními kontrolami; teprve pak nad schválenou lokální kopií historie. Zaznamenat rozsah refs/commitů, merge a binární případy, pravidla a výjimky, čas, zdroje, nálezy, falešně pozitivní i přehlédnuté známé nálezy. Počet nálezů v reálném repu sám neměří citlivost. [Gitleaks v8.30.1 dokumentuje vlastní konfiguraci, výjimky a redakci](https://github.com/gitleaks/gitleaks/blob/83d9cd684c87d95d656c1458ef04895a7f1cbd8e/README.md). Vynucený zákaz sítě musí platit i při chybě konfigurace scanneru; nejde o oprávnění testovat živé klíče.

### P2 — opravit inventuru buildu

Audit ř. 14 uvádí build jen při release. Již na `8774d5e` obsahuje `.github/workflows/ci.yml` běžný krok Build (`pip install build`, `python -m build`) v testovací matici. B7 má převzít i tento existující zdroj. Nový druhý build systém není odůvodněn.

### P2 — reprodukovatelnost a podmíněné závěry před pilotem

Audit uvádí obecné GitHub API zdroje bez konkrétních commitů/blobů, nákladů a rollbacku. Doplnit odkazy na konkrétní licence a analyzované soubory; před stažením/spuštěním pinovat artefakt i hash a závislosti. Část ověřených upstream ref/blob identifikátorů dodává přiložené JSON. Toto nejsou hashe distribuovaných wheelů/binárek ani dokončený audit jejich závislostí.

CRAP vzorec je vhodný pro pilot, ale před výpočtem určit jmenovatel coverage jako spustitelné statements/branches příslušné funkce, nikoli všechny fyzické řádky mezi start/end. Ošetřit nested funkce, async/dekorátory, cesty, zdrojové mapy TS a shodu commitu zdroje s coverage. Chybějící/nepřiřaditelné coverage = N/A; nikoli cov=0 nebo PASS. Ověřit malé ručně spočítané případy (100 %, 0 %, chybějící data). Přínos, režie a případné prahy zůstávají hypotézou do měření. Rollback pilotu: odstranit pouze vlastní scratch, neinstalovat runtime závislost, nepřepisovat politiku, DB ani služby.

## Licenční ověření a rozhodnutí

| Kandidát | Co jsem ověřil | Rozhodnutí pro další návrh |
|---|---|---|
| lizard 1.25.0 | LICENSE.txt na `edbb09b6e962bfcb39e6f81a8e7610633f214967` obsahuje MIT oprávnění a podmínku zachování notice. NOASSERTION je výsledek klasifikátoru, nikoli zákaz. | Licenčně vhodný kandidát pro oddělený dev pilot; zachovat Terry Yin/contributors notice při distribuci, zkontrolovat konkrétní balíček a transitivní závislosti. [Licence](https://github.com/terryyin/lizard/blob/edbb09b6e962bfcb39e6f81a8e7610633f214967/LICENSE.txt). |
| coverage.py 7.17.2 | LICENSE.txt na `d0d3e1d599938f251e9951eab84c0ebea671c6cb` je Apache-2.0. | Vhodný kandidát pro pilot; při distribuci zachovat příslušné license/notice a další povinnosti. Výkon dosud nezměřen. [Licence](https://github.com/coveragepy/coveragepy/blob/d0d3e1d599938f251e9951eab84c0ebea671c6cb/LICENSE.txt). |
| Gitleaks / Betterleaks | MIT LICENSE ověřeny na pinech v evidence JSON. Betterleaks README výslovně uvádí původního autora Gitleaks, proto není potřeba sekundární VPN článek. | Porovnat, zatím žádný vítěz. CLI, pravidla, výjimky, formát a redakci ověřit na konkrétní verzi. [Gitleaks licence](https://github.com/gitleaks/gitleaks/blob/83d9cd684c87d95d656c1458ef04895a7f1cbd8e/LICENSE), [Betterleaks licence](https://github.com/betterleaks/betterleaks/blob/8d77e1712fb06fd14444cc648686f1d5655e28a8/LICENSE), [Betterleaks původ](https://github.com/betterleaks/betterleaks/blob/8d77e1712fb06fd14444cc648686f1d5655e28a8/README.md). |
| Semgrep engine / pravidla | CE LICENSE na `ac37d1f6e47b17648b8f854d6e2f2d72fade2567` je LGPL-2.1. Rules LICENSE na `16b9e6d45f10649f6a531ec4838ed9924372c3bc` odkazuje na Semgrep Rules License 1.0. | Volání externího CE s vlastními nezávisle vytvořenými pravidly lze dále posoudit. Registry pravidla nejsou obecně permisivní: podmínky omezují užití na interní podnikové účely a zakazují distribuci i zpřístupnění jako službu. Nezahrnovat je do MIT produktu ani do služby bez odpovídajícího oprávnění. Odděleně posoudit každé pravidlo a případnou redistribuci engine/deps; nekopírovat pravidla pod záminkou vlastních. [Engine licence](https://github.com/semgrep/semgrep/blob/ac37d1f6e47b17648b8f854d6e2f2d72fade2567/LICENSE), [podmínky pravidel](https://semgrep.dev/legal/rules-license/). |

Gas Town v1.2.1 README a MIT text podporují pouhou inspiraci merge frontou; nejde o audit chování či nemožnosti přímého push. [README](https://github.com/gastownhall/gastown/blob/v1.2.1/README.md), [licence](https://github.com/gastownhall/gastown/blob/v1.2.1/LICENSE). Gas City/Beads/Crewship se nyní neintegrují. Jejich implementaci ani file-level původ jsem úplně neauditoval; hvězdičky a marketing nejsou důkaz stability nebo bezpečnosti. Nejasný Crewship není důvod přijmout kód.

## Rozsah ověření a předání

Statická kontrola přesného commitu a CI/bridge/process_guard, primární licenční texty, verze/ref → commit a Git blob identity přes GitHub read-only konektor. Bez testování cizích binárek, bez pilotu nebo nového měření coverage/CRAP. Pro dokumentační změnu není vydáván nový pytest výsledek; dřívější 586 passed/1 xfailed není důkaz kvality dosud nespouštěného B7.

Po opravě uvedených podmínek má Ada předložit nový přesný commit a konkrétní pilotní příkazy/izolaci. Toto stanovisko nepovoluje změny bezpečnostní konfigurace, globální instalaci, externí přenos nálezů ani merge/push/deploy. UI review #6 mezitím Ada schválila na `6fd6080927c9976a6204a1bf9c4ef2f40197c2df`; samostatná věc, žádná implementace B7 tím nevznikla.
