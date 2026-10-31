# Fáze A: zastavení potomků po pádu bridge

Stav: návrh k implementaci a ověření, nikoli existující záruka. Při ukončení
bridge pomocí SIGKILL se jeho `finally` neprovede. Dohled `process_guard` díky
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

## Navržené řešení pro Linux s cgroup v2

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
v aktuálním stromu. Integrace vyžaduje nastavení a nasazení služby; to zatím
nebylo provedeno. Současný dohled nad procesovou skupinou chrání jen částečně.

Pro izolovaný experiment je potřeba samostatná dočasná uživatelská služba
s delegovanou cgroup, například s tímto nastavením v její `[Service]` sekci:

```ini
Delegate=yes
```

Experiment musí používat výhradně novou testovací databázi a umělé workery;
nemá měnit ani restartovat stávající služby. Skutečné nasazení by vyžadovalo
samostatné posouzení a souhlas. Samotné `Delegate=yes` opravu neimplementuje:
je nutné doplnit správu cgroup a nezávislé obnovení úklidu podle bodů výše.

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
