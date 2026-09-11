# Automatický most mezi Claude Code a Codexem

## Shrnutí

Automatické propojení je technicky proveditelné, ale nemá být postavené jako „psaní do právě otevřených terminálů“. Externí CLI ani jednoho nástroje bezpečně negarantuje vstup do libovolné interaktivní relace, ve které právě sedí člověk. Claude Code ale umí mezi svými živými sessions předávat zprávy nativním nástrojem `SendMessage`; lokálně bylo ověřeno i probuzení nečinné pojmenované session. Interní socketový protokol pod tímto nástrojem není veřejné integrační API a most do něj nesmí psát přímo. Codex App Server umí přidat vstup do aktivního turnu pomocí `turn/steer`, ale pouze v threadu spravovaném danou instancí App Serveru, nikoli obecně v libovolném Codex TUI.[^1][^6]

Doporučené řešení proto odděluje dvě cesty:

1. **Autonomní úkoly** obsluhují specializované, mostem vlastněné sessions Claude Code a Codexu. Ty lze spustit, obnovit, sledovat a po pádu znovu připojit podporovanými rozhraními.
2. **Lidské interaktivní sessions** dostávají oznámení a kontext přes dnešní inbox a lifecycle hook při nejbližším promptu. U Claude Code může jiná Claude session použít nativní `SendMessage`; Codex TUI dál nemá obecný externí vstup.

Nad oběma cestami bude **vlastní live chat agent-lease**: jedna společná místnost pro Michala, Claude a Codex. Chat nebude pouhá vizualizace logu. Bude hlavním místem pro okamžité zprávy, zadávání úkolů, odpovědi agentů a viditelný stav doručení. Automatické adaptéry z něj mohou převzít práci ihned; otevřené lidské sessions si stejnou zprávu převezmou hookem při nejbližší interakci.

Jádrem má být lokální broker s trvalou adresovanou frontou, stavovým automatem doručení, potvrzením výsledku a auditem. Broker bude jediným vlastníkem SQLite databáze a klienti s ním budou mluvit přes Unix socket. Tím se odstraní pětiminutové hlídání, závislost na MCP voláních a přímý zápis sandboxovaných agentů do `~/.agent-lease`.

Toto není mechanismus pro obcházení limitů, oprávnění nebo schvalování. Když poskytovatel oznámí vyčerpaný limit, položka zůstane bezpečně ve frontě do času resetu. Zpráva od druhého agenta je vstup, nikoli nové oprávnění.

## Co bylo ověřeno lokálně

Pracovní prostředí používá WSL2, Ubuntu se zapnutým systemd, Python 3.12.3, Codex CLI 0.153.4 a Claude Code 2.1.268. Soubor `/etc/wsl.conf` obsahuje `systemd=true`. Systemd je tedy vhodný správce procesu během běhu distribuce, ale Microsoft upozorňuje, že systemd služby samy WSL instanci neudržují živou.[^15]

Současný agent-lease ukládá data do SQLite v `~/.agent-lease/room.db`. Tabulka zpráv je broadcastová; nemá příjemce, typ zprávy, stav doručení, počet pokusů, lease na zpracování ani idempotency key. Inbox používá jeden kurzor příjemce a posune jej už při vložení zprávy hookem. To dokládá doručení kontextu, ne dokončení práce.

Lokální testy také ukázaly praktickou sandboxovou asymetrii:

- companion runtime může přepsat Codex permission profile na `workspace-write`;
- zapisovatelný workspace se odvozuje od pracovního adresáře;
- MCP volání může s `approval_policy = "never"` skončit dříve než ekvivalentní lokální CLI;
- pevná databáze mimo aktuální workspace proto nemůže být podmínkou práce agenta.

Claude Code strana byla ověřena přímo přes CLI. `claude agents --json` poskytuje seznam běžících sessions včetně ID, pracovního adresáře a stavu. `claude -p` spustí headless relaci a `--bg -n <jméno>` pojmenovanou relaci na pozadí. Externí CLI nemá příkaz `send`, ale živé Claude sessions si umějí předávat nedůvěryhodné cross-session zprávy přes nativní `SendMessage`. Lokální test 11. 9. 2026 potvrdil, že zpráva probudila nečinnou background session a odpověď se vrátila odesílateli. Dokumentované obnovení sessions je použitelné pro vlastní neaktivní relaci; souběžné obnovení téže sessions ve více terminálech může promíchat zprávy do jednoho transcriptu.[^6][^7]

Na Codex straně je podporovaným integračním bodem App Server. Používá JSON-RPC 2.0, vyžaduje inicializační handshake a nabízí `thread/start`, `thread/resume`, `turn/start`, stavové notifikace a `turn/steer` pro aktivní turn.[^1] Podporuje stdio, Unix socket a experimentální WebSocket transport. Pro tento lokální most je správná volba stdio nebo Unix socket; síťový WebSocket nepřináší užitek ani bezpečnostní výhodu.

## Proč nestačí současné mechanismy

### Lifecycle hooks

Hooks jsou výborné pro vložení čekající zprávy při `SessionStart` nebo `UserPromptSubmit` a pro uvolnění lease při ukončení. Spouštějí se ale jen v rámci životního cyklu existující relace. Nejsou obecný démon a nedokážou samy probudit neaktivního agenta.[^2][^8]

### Plánované úlohy a polling

Pětiminutové dotazování zvyšuje latenci, vytváří zbytečné procesy a stále neřeší potvrzení práce po pádu. Codex automatizace s minutovým plánem nejsou lokální event bus a jejich event triggers jsou určeny jen vybraným konektorům, nikoli libovolné změně v agent-lease.[^3]

### Sledování WAL souboru

Inotify nad SQLite WAL není korektní fronta. Změna souboru neříká, která logická zpráva byla commitnuta, a checkpoint nebo reset WAL může vyvolat další události. SQLite WAL hook je navíc callback konkrétního databázového handle; není to cross-process odběr změn.[^12]

### Přímé ovládání terminálu

`tmux send-keys`, simulace kláves nebo automatické potvrzování dialogů by bylo křehké a zaměnilo by uživatelovu autoritu za text druhého agenta. Takové řešení se rozbije změnou UI, neumí bezpečně poznat stav turnu a může potvrdit operaci, kterou uživatel neschválil.

## Doporučená architektura

```text
 live chat UI ── loopback/SSE ┐
 hooks/CLI ───── Unix socket ─┼── agent-lease broker ─── SQLite
 MCP client ──── Unix socket ─┘        │
                                       ├── Codex adapter ── App Server ── bridge thread
                                       └── Claude adapter ─ SDK/CLI ───── bridge session

 interactive Codex/Claude sessions ← inbox vložený lifecycle hookem
```

### Vlastní live chat

Live chat bude samostatné lokální rozhraní, nikoli automatizace klávesnice v terminálu. Doporučená první verze je malá webová aplikace dostupná pouze na loopback adrese. Příkazy posílá brokeru přes lokální HTTP a nové události přijímá pomocí Server-Sent Events; obousměrný WebSocket není pro první verzi nutný. Nativní CLI/TUI může později používat stejný event stream.

Místnost musí v reálném čase ukazovat:

- zprávy Michala, Claude a Codexu s jednoznačným odesílatelem a příjemcem;
- stavy `queued`, `delivered`, `working`, `done`, `failed` a `needs_review`;
- přítomnost `online`, `idle`, `busy`, `rate-limited` a `offline`, včetně času očekávaného resetu;
- stav `stalled`, když busy turn překročí heartbeat/časový limit; UI na něj upozorní člověka, ale samo proces nezabije;
- běžné konverzační zprávy oddělené od spustitelných tasků;
- odpovědi a výsledky ve vlákně původního úkolu;
- možnost úkol zrušit, bezpečně zopakovat nebo předat člověku;
- trvalou historii po restartu prohlížeče, brokeru i WSL.

Průběžná aktivita smí ukazovat bezpečné stavové události, například „čte soubor“, „spouští testy“ nebo „čeká na limit“. Live chat nebude zobrazovat skryté interní uvažování modelu ani neupravený citlivý výstup nástrojů. Finální odpověď a auditované změny se uloží jako běžné zprávy.

Buzení musí šetřit kvótu modelů. Okamžitě budí pouze adresovaný `task` nebo výslovné oslovení; obyčejný `chat` a `broadcast` zůstávají v inboxu. Rychle po sobě jdoucí požadavky se přibližně pět sekund seskupují, každý adaptér má hodinový strop probuzení a levnější bridge session má přednost před drahou interaktivní relací.

Každá odeslaná položka musí mít výslovný režim:

- `chat`: informace pro místnost bez automatického oprávnění měnit soubory;
- `task`: požadavek k převzetí konkrétním agentem s capability profilem;
- `broadcast`: oznámení oběma agentům;
- `control`: zrušení, retry nebo předání již existujícího tasku.

Tím se zabrání tomu, aby obyčejná věta v konverzaci nečekaně spustila změny v projektu. UI může nabídnout tlačítko „Zadat jako úkol“, ale nesmí samo odhadovat rozšíření oprávnění.

Prohlížeč neumí přímo používat Unix socket, takže webové UI vyžaduje loopback listener. Ten musí poslouchat výhradně na `127.0.0.1`/`::1`, používat náhodný session token, kontrolu `Origin`, ochranu proti CSRF a žádné CORS pro cizí weby. Vzdálený přístup, cloudová synchronizace a otevření do LAN nejsou součástí první verze.

Identita `michal` je rezervovaná pro požadavky ověřené session tokenem webového UI. Hodnota `AGENT_NAME=michal` z CLI, MCP nebo procesu adaptéru nesmí založit zprávu s lidskou autoritou; broker takový požadavek odmítne nebo uloží pod skutečnou strojovou identitou.

### Broker

Broker je dlouho běžící lokální služba a jediný proces, který otevírá SQLite pro zápis. Poskytuje malé RPC API přes Unix domain socket s právy `0600`. Každý příkaz nejprve atomicky uloží zprávu nebo změnu stavu, teprve potom probudí příslušný adaptér. Po restartu broker znovu projde nedokončené položky, takže socket řeší nízkou latenci a databáze trvanlivost.

Tento krok současně řeší sandbox: agent nepotřebuje přímý přístup k databázi. Potřebuje pouze spustit tenkého klienta a dosáhnout na socket. Umístění socketu musí být konfigurovatelné a ověřené příkazem `doctor` pro každý způsob spuštění. Companion ani pracovní adresář se nesmí tiše považovat za kompatibilní.

Systemd user unit může používat `Restart=on-failure`, což systemd doporučuje pro dlouho běžící služby.[^14] Broker se hodí i pro socket activation.[^13] Adaptéry mohou být samostatné služby, aby pád nebo limit jednoho poskytovatele nezastavil druhého.

### Codex adaptér

Adaptér vlastní jednu nebo více explicitně označených bridge sessions. Spustí App Server přes stdio nebo lokální Unix socket, provede `initialize`/`initialized` a uloží `threadId`. Když je thread neaktivní, vytvoří `turn/start`; běžné zprávy během aktivního turnu nechá čekat. `turn/steer` se použije pouze pro výslovně urgentní doplnění téhož úkolu, protože mění vstup již běžící práce.[^1]

App Server zveřejňuje stav threadu a turnu a umí načíst údaje o limitech včetně času resetu.[^4] Adaptér proto nemá hádat čekací dobu: nastaví `not_before` podle poskytovatele a broker položku později znovu nabídne.

Codex sandbox musí zůstat explicitní: `workspaceWrite`, přesné zapisovatelné kořeny a ve výchozím stavu vypnutá síť.[^5] Bridge nepoužije MCP jako jedinou řídicí cestu, protože v aktuálním profilu mohou být MCP calls zrušeny politikou schvalování; lokální RPC klient musí fungovat samostatně.

### Claude Code adaptér

Pro autonomní práci adaptér používá vlastní, bridge-managed session, nikoli relaci otevřenou uživatelem. Preferovaný lokální prototyp je pojmenovaná `claude --bg -n <jméno>` session: běží v plném interaktivním harnessu, je adresovatelná přes nativní `SendMessage` a člověk ji může otevřít pomocí `claude attach`. Agent SDK zůstává pozdější alternativou pro prostředí, kde je potřeba veřejné programové API.[^7][^9]

Je nutné počítat s licencováním a kvótami podle skutečně zvoleného runtime a přihlášení. SDK nabízí rate-limit události se stavem a časem resetu; CLI varianta musí používat stav, který poskytuje Claude harness. Adaptér podle něj odloží úkol místo agresivního retry.[^11] Nikdy současně neotevře bridge session v interaktivním terminálu. Když zjistí jiného vlastníka nebo nejasný stav relace, položku přesune do `needs_review`.

Pro uživatelovu otevřenou Claude session zůstává druhá cesta: broadcast nebo adresovaná poznámka se vloží hookem při příštím promptu. To je upozornění, ne automatické provedení.

## Model zpráv a doručení

Současné `say` lze zachovat jako zpětně kompatibilní broadcast typu `note`. Nová vrstva potřebuje alespoň tyto entity:

- `messages`: stabilní ID, odesílatel, příjemce, `kind` (`note`, `task`, `result`, `cancel`), payload, projekt/CWD, `created_at`, `not_before`, expirace, `dedupe_key` a `reply_to`;
- `deliveries`: zpráva, příjemce, stav, počet pokusů, lease token, `lease_until`, provider session/turn ID, poslední chyba a časy potvrzení;
- `sessions`: agent, projekt, adaptér, externí session/thread ID, vlastník, stav busy/idle, reset limitu a poslední heartbeat;
- append-only audit všech změn stavu.

Schéma se verzionuje pomocí SQLite `user_version` a migruje transakčně.[^16]

Stavový automat:

```text
pending → leased → started → succeeded
    │        │         ├────→ failed → pending (backoff)
    │        │         └────→ needs_review
    └────────┴───────────────→ expired / cancelled / dead_letter
```

Garance je **at-least-once delivery**, nikoli „exactly once“. Pád po přijetí turnu poskytovatelem, ale před lokálním potvrzením, vytváří nejasný stav. Broker proto uloží záměr před odesláním a externí session/turn ID hned po přijetí. Po restartu se Codex adaptér pokusí stav srovnat s historií threadu. Pokud Claude strana neumí bezpečně rozhodnout, nesmí úkol slepě zopakovat; přejde do `needs_review`. Stabilní `dedupe_key` bude také součástí promptu a výsledku.

Potvrzení nastane až po dokončení provider turnu a uložení výsledku. Pouhé zobrazení v hooku nesmí posouvat kurzor pracovního úkolu.

## Bezpečnostní hranice

Zpráva od agenta je nedůvěryhodný vstup. Nemůže rozšířit původní uživatelovo oprávnění. Každý task proto nese explicitní profil schopností, například `read`, `edit` na konkrétních cestách, `test` nebo `commit`. Push, publikování, mazání, změny externích služeb a jiné materiálně širší akce jsou ve výchozím stavu zakázané a vyžadují člověka.

Claude adaptér nesmí používat `bypassPermissions`; vhodný je `dontAsk` s předem povolenými nástroji nebo vlastní callback, který odmítne vše mimo scope.[^17] Codex dostane přesné writable roots a zakázanou síť, pokud úkol výslovně nevyžaduje jinak.[^5]

Další minimální ochrany:

- socket `0600`, ověření stejného unixového uživatele a případně `SO_PEERCRED`;
- per-adapter capability token v souboru `0600`; samotné `AGENT_NAME` není autentizace;
- limity velikosti zprávy, TTL, validace schématu a redakce secretů v logu;
- jedna aktivní write práce na agenta a projekt;
- write úkoly přednostně v izolovaném git worktree; práce ve sdíleném main tree jen opt-in;
- automatické zprávy do aktivního turnu pouze jako explicitní pokračování stejného tasku.

## SQLite a souběh

WAL dovoluje souběh čtenářů s jedním zapisovatelem, stále však existuje vždy jen jeden writer.[^18] Broker jako jediný vlastník DB proto není jen architektonické zjednodušení; omezuje konflikty, centralizuje transakce a dovolí spolehlivé probuzení workerů po commitu.

Před rozšířením souběhu je nutný upgrade SQLite. Oficiální dokumentace uvádí vzácnou WAL-reset chybu zveřejněnou 3. března 2026, která zasahuje verze 3.7.0 až 3.51.2 a je opravena ve 3.51.3 a backportech 3.50.7 a 3.44.6.[^18] Lokální projektový Python hlásí SQLite 3.50.4 a systémový Python 3.45.1, tedy verze v uvedeném rozsahu. Nejbezpečnější je současně aktualizovat SQLite a zabránit více procesům v přímém zápisu.

## Provoz na WSL2

User služby mohou broker a adaptéry restartovat při chybě a spustit při startu dané systemd user instance. Neřeší však vypnutou distribuci ani restart Windows. Microsoft výslovně říká, že systemd služby WSL instanci neudrží živou.[^15]

Proto existují dvě poctivé provozní úrovně:

1. **Automatické během běhu WSL**: systemd user units, restart po pádu, žádný pětiminutový polling.
2. **Automatické po přihlášení do Windows**: samostatná, pozdější integrace přes Windows Task Scheduler nebo Startup, která spustí WSL a bridge. Tato část musí být testována přímo z Windows a nemá být skrytě součástí linuxového instalátoru.

## Implementační plán

### Fáze 0 — kontrakt a preflight

Zmrazit terminologii, ownership sessions a bezpečnostní scope. Přidat pouze `doctor`, který ověří cestu k socketu, přístup ze všech launcherů, verzi SQLite, systemd/WSL, dostupnost Codex App Serveru, Claude SDK přihlášení/kvótu a konflikt živé session. Bez automatického provádění.

### Fáze 1 — trvalá adresovaná fronta

Přidat migrace, `messages`/`deliveries`/`sessions`, příkazy `send --to`, `jobs`, `ack`, `retry` a `cancel`. Přidat `wait --for <agent> [--since <id>] [--timeout s]`: blokující lokální čekání na adresovanou zprávu, které může harness spustit na pozadí bez spotřeby modelových tokenů. Zachovat současné hooks a `say`. Otestovat atomické claimování, expiraci lease, backoff a zpětnou kompatibilitu.

### Fáze 2 — broker a Unix socket

Přesunout veškerý DB přístup za broker, převést CLI/MCP/hooks na tenké klienty, zavést socket permissions a restart recovery. Současně opravit SQLite runtime. Teprve potom přidat push probuzení adaptérů.

### Fáze 2b — live chat

Přidat lokální webovou místnost, HTTP API pro příkazy a SSE stream událostí. UI nejprve připojit k testovacím/falešným adaptérům a ověřit reconnect, obnovení historie, pořadí zpráv, stavové přechody, bezpečné rozlišení `chat` versus `task` a zneplatnění session tokenu. Až potom připojit skutečné modely.

### Fáze 3 — Codex proof of concept

Vyhrazený bridge thread přes App Server; nejprve pouze read-only úkoly. Ověřit init, resume, busy stav, limit, pád přesně před a po `turn/start` a kompatibilitu s `approval_policy = "never"` bez závislosti na MCP.

### Fáze 4 — Claude proof of concept

Vyhrazená Claude session přes oficiální SDK nebo podporované headless CLI; nejprve read-only. Ověřit oddělenou kvótu, resume po restartu, zákaz souběžného attach/resume, limit eventy a zobrazení výsledku člověku.

### Fáze 5 — řízené změny kódu

Přidat path leases, capability profiles, izolované worktrees, testování a strukturovaný result/hand-off. Write režim zapnout až po crash a prompt-injection testech.

### Fáze 6 — Windows bootstrap a provozní UX

Volitelný Windows startup, desktopová upozornění, health dashboard, rotace logů, dead-letter workflow a dokumentovaný recovery postup.

## Akceptační kritéria

První produkčně použitelná verze musí splnit:

- zpráva se během běhu WSL dostane k idle adaptéru typicky do dvou sekund; přechodné `wait` smí interně čekat s nízkou frekvencí, cílový broker bude event-driven;
- pending práce přežije restart brokeru i adaptéru;
- crash testy v bodech „před send“, „provider accepted“ a „před ACK“ nevedou k tichému dvojímu provedení;
- nikdy nevznikne souběžný vlastník jedné Claude/Codex bridge session;
- rate limit nastaví odklad podle poskytovatele a nezpůsobí retry storm;
- nikdy nebudou oba agenti čekat jeden na druhého bez nastraženého probuzení;
- `chat` a `broadcast` samy nebudí drahý modelový tah; adresované tasky se krátce dávkují a respektují hodinový strop probuzení;
- busy session bez heartbeat přejde viditelně do `stalled`, upozorní člověka a není automaticky zabita;
- pouze autentizované loopback UI smí vystupovat jako `michal`; proměnná prostředí ani agentí zpráva lidskou autoritu nevytvoří;
- žádný externě dosažitelný listener; interní RPC socket je `0600` a webový chat poslouchá jen na loopbacku s autentizačním tokenem;
- live chat obnoví event stream bez ztráty či zdvojení viditelných zpráv a jasně rozlišuje konverzaci od spustitelného tasku;
- výchozí agent nemá síť ani zápis mimo scope;
- řešení funguje i s Codex `approval_policy = "never"`;
- nejasná práce je viditelná v `needs_review`, nikoli skrytě zahozena nebo zopakována;
- interaktivní uživatel vždy pozná, zda zpráva skončila v jeho inboxu, nebo ji provedla oddělená automatická session.

## Rozhodnutí

Doporučení je pokračovat, ale po vrstvách. Nejprve adresovaná trvalá fronta a jediný DB broker; potom samostatné adaptéry. Největší technická rizika nejsou samotné spuštění procesů, ale hranice mezi interaktivní a automatickou session, nejasný výsledek po pádu a sandboxový přístup k pevnému úložišti.

První verze nemá slibovat „Claude a Codex si píšou do právě otevřených oken“. Má nabídnout vlastní společný live chat a slíbit přesnější a užitečnější věc: zpráva je vidět okamžitě, zadaný úkol nezmizí, oprávněná automatická session jej bezpečně převezme, limit jej pouze odloží, výsledek se vrátí do místnosti i auditu a člověk jej uvidí také ve své interaktivní session při nejbližší interakci.

## Zdroje

[^1]: OpenAI, [Codex App Server](https://learn.chatgpt.com/docs/app-server) — transporty, handshake, threads, turns, `turn/steer` a notifikace.
[^2]: OpenAI, [Codex hooks](https://learn.chatgpt.com/docs/hooks) — lifecycle události a omezení MCP hooků.
[^3]: OpenAI, [Codex automations](https://learn.chatgpt.com/docs/automations) — plánované úlohy a dostupné event triggers.
[^4]: OpenAI, [Codex App Server: rate limits](https://learn.chatgpt.com/docs/app-server#rate-limits) — čtení a notifikace limitů.
[^5]: OpenAI, [Codex App Server: sandbox policy](https://learn.chatgpt.com/docs/app-server#sandbox-policy) — `workspaceWrite`, writable roots a síťová politika.
[^6]: Anthropic, [Claude Code sessions](https://code.claude.com/docs/en/sessions) — resume sessions a riziko souběžných terminálů.
[^7]: Anthropic, [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage) — headless, continue a resume režimy.
[^8]: Anthropic, [Claude Code hooks](https://code.claude.com/docs/en/hooks) — `SessionStart`, `UserPromptSubmit` a další lifecycle hooks.
[^9]: Anthropic, [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) a [Python SDK](https://code.claude.com/docs/en/agent-sdk/python) — programatické sessions, klient a změna účtování SDK.
[^10]: Anthropic, [Modifying system prompts](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts) — minimální default a `claude_code` preset.
[^11]: Anthropic, [Agent SDK Python reference](https://platform.claude.com/docs/en/agent-sdk/python) — `RateLimitEvent` a `resets_at`.
[^12]: SQLite, [Write-Ahead Log hook](https://www.sqlite.org/c3ref/wal_hook.html) — callback vázaný na databázové spojení.
[^13]: systemd, [systemd.socket](https://github.com/systemd/systemd/blob/main/man/systemd.socket.xml) — Unix socket activation a režim socketu.
[^14]: systemd, [systemd.service](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml) — doporučení `Restart=on-failure`.
[^15]: Microsoft, [Use systemd to manage Linux services with WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd) — aktivace systemd a fakt, že služby neudrží WSL instanci živou.
[^16]: SQLite, [Database File Format: user_version](https://www.sqlite.org/fileformat.html#the_user_version_number) — aplikační verze schématu.
[^17]: Anthropic, [Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions) — permission modes, `dontAsk` a rizika `bypassPermissions`.
[^18]: SQLite, [Write-Ahead Logging](https://www.sqlite.org/wal.html) — souběh, jeden writer, checkpointing a oprava WAL-reset chyby zveřejněné v březnu 2026.
