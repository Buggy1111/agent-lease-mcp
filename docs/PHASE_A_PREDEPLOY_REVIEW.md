# Přednasazovací kontrola fáze A

Datum: 1. 11. 2026. Kontrolovaný kód: `f918cf610d1c0c001c8ac45174680486f18f6548`.
Jde o kontrolu provedenou autorem změn, nikoli nezávislé schválení fáze.
Provozní služby nebyly změněny. Osmihodinový běh má vlastní neměnný snapshot
`/tmp/agent-lease-watchdog-chaos/final8h`; tyto přípravy jej nemění.

## A1–A10: implementace versus požadavek

| Bod | Zjištění a důkazy | Co brání úplnému odškrtnutí |
|---|---|---|
| A1 | Token a vlastník oddělují interaktivní a bridge cestu; testy `test_interactive_accept_after_bridge_lease_is_refused`, `test_bridge_never_takes_a_task_the_interactive_agent_accepted`. | Odchylka od doslovné roadmapy: `accept` s tokenem atomicky přechází do `started`; není pouze potvrzením příjmu. Je potřeba výslovně zrevidovat tuto sémantiku, ne označit původní text za splněný. |
| A2 | Kontroly deadline v `ack`, `holds`, `extend_lease`; `test_expired_lease_cannot_be_acked_or_extended`. | Nezávislé review a provozní ověření. |
| A3 | Zapisující pokus po limitu přechází k prověření; `test_rate_limit_after_start_with_write_access_is_not_retried`. | Výslovně deklarované read-only běhy lze odložit a opakovat. Bez důvěryhodného read-only omezení to není obecná záruka proti opakovaným účinkům. |
| A4 | `complete` ukládá stav a výsledek v jedné transakci, porovnává token a hash výsledku. Crash testy před a po COMMIT jsou v `tests/test_crashpoints.py`. | Lokální transakce neprokazuje právě-jednou provedení vnější služby. |
| A5 | Cgroup, hlavní strážce, nezávislý dohled, trvalá pojistka, obnova; čtyři pádové kombinace a držená roura ověřeny v dočasných scope. | Cgroup není zapnuta v běžících službách. Bez ní zůstává známý `xfail` odděleného potomka. Chybí dokončení aktuálního 8h testu a provozní kontrola. |
| A6 | Checkpoint ukládá pokus, HEAD, otisk a důkazy; testy `test_checkpoint_needs_a_valid_lease_and_reaches_the_next_attempt`, `test_matching_workspace_checkpoint_still_requires_effect_verification`. | Obnova je poradní kontext. Při neshodě se checkpoint nezpracuje jako automatický plán, ale může být předán jako informace k ověření. Není implementována transakční obnova každého kroku s garancí neopakování účinku. Doslovný požadavek roadmapy proto není hotový. |
| A7 | Broker provádí reconciliaci i bez `lease_next`; `test_reconcile_expires_leases_of_every_recipient`. | Nezávislé review a nasazení. |
| A8 | DELETE režim a offline migrace jsou implementované; opravená SQLite 3.54.1 prošla níže popsanou oddělenou zkouškou. | Runtime testovaného balíčku stále používá SQLite 3.50.4. A8 vyžaduje opravenou knihovnu ve službách i MCP serveru; workaround není náhrada tohoto požadavku. |
| A9 | Povinný screening, schválení vázané na text/adresáta/pokus; testy `test_quarantine_holds_for_every_path_to_start`, `test_quarantine_approval_is_single_use_and_bound_to_recipient`. | Heuristika není hranice proti všem podvrženým instrukcím ani proti stejnému OS uživateli. |
| A10 | Pauza ovlivňuje převzetí a prodloužení; spouštění má bariéru. Testy `test_pause_between_final_check_and_spawn_must_not_start_work`, `test_pause_waits_for_launch_barrier_across_processes`. | Callable read-only runner nelze násilně ukončit jako subprocess. Je potřeba ověřit skutečné služby a UI. |

Testy vyjmenované bez jiné cesty jsou v `tests/test_bridge.py`. Zelená sada
testů není nezávislé review. Předchozí dlouhé běhy se nesmějí vydávat za důkaz
aktuálního buildu. Stav fází ani jejich schvalovací soubor nebyl posunut.

## A8: oddělený experiment

- Dostupné Python 3.11/3.13/3.14 z uv používají SQLite 3.50.4; systémový Python
  3.12.3 používá 3.45.1. Ani jeden není řešením A8.
- Lokálně uložená knihovna z APSW má SQLite 3.54.1. Před zkopírováním do
  `/tmp/agent-lease-audit-prep/sqlite/libsqlite3.so` byl ověřen hash a velikost
  proti záznamu RECORD. SHA-256 kopie:
  `168e48ea59c72fcbe3c4d5a92dcffc4f338775e3879265f065198c617e4ac1ae`.
- `LD_PRELOAD` tuto knihovnu do uv Pythonu 3.13 nezapojil: Python i SQL dotaz
  nadále hlásily 3.50.4. Tento pokus se nepovažuje za úspěch.
- V samostatném systémovém Pythonu 3.12.3 hlásí `sqlite3.sqlite_version` i
  `SELECT sqlite_version()` hodnotu 3.54.1. V syntetické databázi prošlo
  `Store.complete`, jeho idempotence, záloha, `integrity_check`, rollback po
  SIGKILL zapisujícího procesu a offline migrace WAL → DELETE.
- Výsledek: `/tmp/agent-lease-audit-prep/sqlite-result.json`; testovací skript:
  `/tmp/agent-lease-audit-prep/sqlite_probe.py`. Živá DB nebyla otevřena.

To je experimentální cesta k dalšímu testu, ne hotové produkční prostředí.
Chybí úplné prostředí Pythonu 3.12 se správnými nativními závislostmi, ověření
MCP klientů, serveru, brokeru a bridge v jednotném runtime a plán distribuce
knihovny. LD_PRELOAD nebyl nastaven globálně ani ve službách.

Oficiální popis opravy: https://www.sqlite.org/wal.html a
https://www.sqlite.org/releaselog/3_51_3.html .

## Připravený postup nasazení — neprovedeno

1. Nejdříve vyřešit výše uvedené odchylky A1/A6, A8 a výsledky aktuálního testu;
   získat nezávislé review. Následně požádat Michala o přímý souhlas s konkrétní
   instalací a restartem služeb podle jeho instrukcí.
2. Před odstávkou zjistit verze a cesty všech MCP klientů i služeb bez čtení
   souborů s tajemstvími. Zaznamenat původní balíček, interpreter a unit/drop-in
   soubory pro návrat. Připravit ověřenou konzistentní zálohu DB.
3. Potvrdit, že neběží žádný úkol. Koordinovaně zastavit všechny klienty DB,
   bridge, wake, web a broker; samotné zastavení brokeru nestačí. Neprovádět
   offline migraci za běhu ostatních klientů.
4. Ověřit integritu kopie, migrovat pouze při skutečně zastavených klientech.
   Instalovat přesný otestovaný balíček a uzamčené závislosti z offline
   wheelhouse. Zachovat původní prostředí pro návrat.
5. Pro bridge připravit delegaci cgroup a `--cgroup`; explicitně zkontrolovat
   chování KillMode, Restart a oprávnění. Úpravy unit souborů vyžadují souhlas.
6. Spustit broker a následně služby; `doctor --json` musí potvrdit runtime,
   verzi SQLite a dostupnost služeb. Každý MCP klient musí používat stejné
   opravené prostředí; staré procesy se samy aktualizací souborů nepřepnou.
7. Teprve po samostatném schválení živých zpráv otestovat syntetický úkol,
   wait, karanténu a stop/resume. Neodesílat test automaticky skutečnému agentovi.

### Návrat zpět

Při chybě zastavit nové procesy a ověřit prázdné cgroup. Zachovat vadnou DB
pro vyšetření. Vrátit původní interpreter, balíček a konfiguraci; DB obnovovat
jen z ověřené zálohy při zastavených klientech a po rozhodnutí o účincích úkolů.
Obnova starší DB může ztratit záznam o už provedených účincích, proto se její
pending úkoly nesmějí automaticky spustit. Pojistky nemažeme pro obejití chyby.

## A11: checklist společného Windows/WSL testu

Provádět až po schváleném nasazení, mimo osmihodinový test. `wsl --shutdown`
by současný test ukončil a ovlivní i jiné WSL procesy; vyžaduje Michala.

- [ ] Uložit rozpracovanou práci a zaznamenat čas, build, stav fronty a služeb.
- [ ] Ověřit nulové běžící úkoly; nezkoušet restart během reálných vnějších účinků.
- [ ] Z Windows provést `wsl --shutdown`, pak spustit příslušnou distribuci.
- [ ] Ověřit automatický start brokeru/bridge/wake a `doctor --json`.
- [ ] Porovnat frontu, pending/needs_review, integrity_check a zbylé pojistky.
- [ ] Se zvláštním souhlasem provést jeden syntetický živý úkol a ověřit jediné dokončení.
- [ ] Ověřit přihlášení/start Windows podle způsobu skutečného používání.
- [ ] Uložit logy a podpis výsledku člověkem. Bez skutečného provedení A11 neodškrtávat.
