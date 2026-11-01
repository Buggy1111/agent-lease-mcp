# Fáze A: zastavení potomků po pádu bridge

Stav: volitelná cgroup izolace je rozpracovaná, nejde o úplnou záruku. Při ukončení
bridge pomocí SIGKILL se jeho `finally` neprovede. Ve výchozím režimu dohled
`process_guard` díky
`PR_SET_PDEATHSIG` ukončí běžnou procesovou skupinu, ale skupina nezachytí
potomka, který zavolá `setsid`; nezávislý dohled také může sám havarovat.
Bez další hranice proto nelze tvrdit,
že po pádu bridge nebo při pauze nezůstane běžet žádný proces úkolu.
Reprodukce je v `tests/test_crashpoints.py`: známý nedostatek je označen `xfail`,
test po sobě odděleného potomka bezpečně ukončí.

CLI bridge používá sdílenou bariéru mezi pauzou a skutečným spuštěním příkazu.
Pauza se zapíše před čekáním na dokončení rozbíhaného spuštění; pokud se bariéru
nepodaří včas získat, nepotvrdí se jako úspěšná. Test v `tests/test_bridge.py`
ověřuje původní závod a časový limit. Volatelné read-only runnery běží ve vlákně,
které není možné násilně zastavit, a nejsou touto bariérou dosud pokryté.

## Trvalá pojistka před dalším spuštěním

CLI bridge před spuštěním příkazu zapíše a synchronizuje soubor
`.room.db.run-fence-<sha256-jména-agenta>` vedle databáze (skutečný název
databáze může být jiný). Existující soubor brání dalšímu spuštění stejného
agenta i po restartu bridge. Po potvrzeném úklidu se odstraní; při pádu nebo
nejistém úklidu zůstane. Současné starty stejného agenta odděluje atomické
vytvoření souboru. Chyba zápisu vede k zastavení před spuštěním příkazu.
Pojistka platí jen při použití stejné databázové cesty a sdíleného souborového
systému; přesun databáze, její obnova ze zálohy nebo spuštění jiným jménem
agenta ji nepřenese automaticky.

Pojistka není automatický úklid ani ochrana před agentem, který má oprávnění
soubor odstranit. Po běžném dokončení navíc současný dohled nedokáže najít
potomka, který opustil procesovou skupinu a zavřel zděděný výstup. Odstranění
zůstalé pojistky proto vyžaduje nejdříve nezávisle ověřit ukončení všech
procesů původního pokusu a rozhodnout o jeho možných účincích. Soubor nemažte
pouze proto, abyste obešli zablokování. U staré pojistky bez cgroup záznamu nebo
při neověřitelné identitě cgroup je vždy potřeba ruční prověření.

## Rozpracované řešení pro Linux s cgroup v2

Bridge má volitelný režim `--cgroup`: ve vlastní delegované cgroup pro každý
příkaz ověří dostupnost ovládání před spuštěním a po běhu volá `cgroup.kill`
a kontroluje `populated 0`. Strážce volá `cgroup.kill` také při smrti bridge.
Nezávislý dohled mimo skupinu sleduje životnost bridge přes rouru i linuxový
signál smrti rodiče a po jeho zániku také ukončí skupinu. Signál pomáhá i při
nechtěně zděděné kopii roury; Linux jej váže na zakládající vlákno. Pokud dohled selže během běhu, bridge zastaví úkol,
ponechá pojistku a předá úkol ke kontrole. Při současné smrti všech tří procesů
uklidí skupinu až náhradní bridge po restartu.
Není-li delegace dostupná, příkaz se nespustí. Před spuštěním se do pojistky
uloží identita skupiny a bridge drží zámek. Nový bridge může po zániku vlastníka
uklidit jen zaznamenanou skupinu ve své současné delegované cgroup; ověří její
identitu i vyprázdnění. Kontrolní součet chrání před náhodným poškozením záznamu,
ne před jeho úmyslnou změnou oprávněným uživatelem. Starý úkol zůstává nejistý a po vypršení lease přejde
do `needs_review`. Pokud se skupinu nepodaří ověřit nebo uklidit, pojistka
zůstává. Režim není dosud zapnutý ve službách. Proces se stejným uživatelem a
oprávněním ke správě nadřazené cgroup může izolaci obejít.

V dočasné delegované scope byl reálný bridge s umělým příkazem ověřen při
normálním dokončení i po `SIGKILL` bridge: oddělený potomek byl ukončen.
Experiment `tests/chaos/probe_cgroup_bridge.py` nepoužívá živé workery ani
databázi. To samo nedokazuje připravenost k nasazení.

Experiment `tests/chaos/probe_cgroup_recovery.py` v samostatných scope ověřil
úklid při pádu bridge a jednoho dohledu, úklid až po restartu při pádu všech
tří procesů a bezpečné zastavení při samostatném selhání nezávislého dohledu.
Původní úkol nepovažuje za dokončený a živého vlastníka neuklízí.
Obnova funguje jen pod stejnou delegovanou cgroup; při jiné cestě nebo poškozeném
záznamu selže bezpečně a ponechá pojistku k ručnímu prověření.
`tests/chaos/probe_watchdog_leaked_pipe.py` navíc ověřil ukončení skupiny,
i když jiný umělý proces držel otevřenou kopii dohledové roury.

1. Každý pokus spustit ve vlastní cgroup pod službou s delegovanou podskupinou.
   Než se spustí cizí kód, musí být proces do podskupiny zařazen; při chybě
   založení nebo zařazení se úkol nesmí spustit. Skupinu pojmenovat identitou
   pokusu, ne jen PID, a její identitu před spuštěním uložit spolu se stavem.
2. Nezávislý dohled musí znát cgroup pokusu a při ztrátě bridge, zrušení nebo
   pauze vyvolat `cgroup.kill`. Po restartu musí před dalším vykonáváním najít
   zbylé cgroup a uklidit je. Přítomnost nebo nepřítomnost PID nestačí: ověřit
   `cgroup.events` a stav `populated 0`, teprve potom cgroup odstranit.
3. Nejde-li ověřit ukončení, ponechat pokus v `needs_review`, další pokus ani
   automatický start nepovolit. Samotný reset databázového lease není důkaz, že
   proces skončil. Smrt samotného dohledu musí také vést k obnově a úklidu, ne
   k tichému spuštění jiného pokusu.
4. Popsat hranici důvěry: oddělení agentů od cgroup manažera a databáze musí
   zabránit přepisům ze strany agenta. Stejný uživatel s právy do těchto míst
   zůstává schopný ochranu obejít. Na prostředí bez správně delegované cgroup
   se autonomní režim nesmí tvářit, že poskytuje stejnou garanci.

Toto prostředí má cgroup v2, ale aktuální proces nemá právo vytvářet cgroup
v aktuálním stromu. Zapnutí vyžaduje nastavení a nasazení služby; to zatím
nebylo provedeno. Výchozí dohled nad procesovou skupinou chrání jen částečně.

Pro izolovaný experiment je potřeba samostatná dočasná uživatelská služba
s delegovanou cgroup, například s tímto nastavením v její `[Service]` sekci:

```ini
Delegate=yes
```

Experiment musí používat výhradně novou testovací databázi a umělé workery;
nemá měnit ani restartovat stávající služby. Skutečné nasazení by vyžadovalo
samostatné posouzení a souhlas. Samotné `Delegate=yes` nestačí: je také nutný
režim `--cgroup` a ověření obnovy a provozního nastavení podle bodů výše.

Připravený zkušební skript `tests/chaos/probe_cgroup.py` nepoužívá databázi ani
živé workery: v nové podskupině vytvoří umělého potomka, ten zavolá `setsid`,
a skript ověří, že `cgroup.kill` ukončí oba procesy. Navržený příkaz k provedení
**až po souhlasu se spuštěním dočasné uživatelské systemd scope** (z kořene repa):

```sh
systemd-run --user --scope --property=Delegate=yes \
  "$PWD/.venv/bin/python" "$PWD/tests/chaos/probe_cgroup.py"
```

Bez delegace skript skončí bez spuštění procesů. Po výslovném Michalově souhlasu
proběhl také izolovaný test v dočasné uživatelské scope s `Delegate=yes`:
`cgroup.kill` ukončil umělý hlavní proces i jeho potomka po `setsid`; scope
pak byla ve stavu `inactive/dead`. Test nepoužil živou databázi ani existující
služby. Experiment potvrzuje dostupnost mechanismu v této konfiguraci, nikoli
implementaci nezávislého dohledu nebo připravenost k nasazení.

Další izolované experimenty (`tests/chaos/probe_cgroup_parent_death.py` a
`tests/chaos/probe_cgroup_watchdog.py`) ověřily, že dohled po smrti rodiče
uklidí i potomka po `setsid`. Při současném zabití rodiče a jednoho ze dvou
nezávislých dohledů druhý dohled cgroup uklidil. Při zabití rodiče i obou
dohledů potomek zůstal běžet až do úklidu testovacím skriptem. Dva dohledy
samotné tedy nestačí: obnova musí mít další nezávislou hranici a před dalším
spuštěním ověřit vyprázdnění cgroup. Tyto experimenty používají pouze umělé
procesy a nejsou integrací do bridge.

## Nutné ověření před označením za hotové

- V izolované službě spustit potomka, který vytvoří další proces a zavolá
  `setsid`; zabít bridge v několika bodech před spuštěním i během běhu a ověřit,
  že po úklidu cgroup není žádný potomek živý.
- Stejně ověřit pauzu, zrušení, pád dohledu, restart brokeru a opakovaný restart
  služby. Ověřit odmítnutí startu, když cgroup nejde vytvořit, zapsat nebo ověřit.
- Zátěžový test musí kontrolovat účinky a živé cgroup během běhu, nejen PID
  přímého procesu na konci. Restart Windows/WSL zůstává samostatnou zkouškou.
- Pro externí účinky použít podporu idempotence nebo transakční spolupráci cíle;
  systém sám z neznámého stavu nemůže prokázat přesně jedno provedení.
