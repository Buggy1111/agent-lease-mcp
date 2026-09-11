# agent-lease-mcp

Dva coding agenti (Claude Code a Codex) ve stejném repu si nemají šlapat po
souborech. Tenhle MCP server jim dá **nájmy na cesty, přehled o sobě navzájem
a společnou nástěnku** — a nájem se **vynucuje hookem**, ne dobrou vůlí.

## Proč to vzniklo

9. 9. 2026, `silikon-manager`. Koordinační soubor `docs/AGENT-HANDOFF.md`
existoval a oba agenti o něm věděli. Ve 22:28 si stejně oba sáhli na
`deploy/make-work-package.sh` — jeden ho editoval, druhý ho zároveň spouštěl.
Bash čte skript průběžně, text se posunul pod běžícím procesem a místo 3,7GB
instalačního balíčku vypadl nepoužitelný 198MB zmetek. Stálo to hodinu a nasazení
u zákazníka další den.

Poučení, na kterém stojí celý projekt: **koordinace, kterou lze ignorovat, bude
ignorována.** Ne ze zlé vůle — agent prostě pracuje a na nástěnku se nepodívá.

## Čím se to liší od hotových řešení

| | zámky | vynucení | TTL | přítomnost | vzkazy | audit |
|---|---|---|---|---|---|---|
| [AgentRoom](https://arxiv.org/html/2608.23740v1) (paper) | ✅ | jen prompt | ? | ✅ | ✅ | ✅ |
| [Agent Claim MCP](https://glama.ai/mcp/servers/vk0dev/agent-claim-mcp) | ✅ | jen prompt | ✅ | ❌ | ❌ | ❌ |
| [agent-orchestration](https://github.com/madebyaris/agent-orchestration) | ✅ | jen prompt | ❌ | ✅ | ✅ | ❌ |
| **agent-lease-mcp** | ✅ | **PreToolUse hook** | ✅ | ✅ | ✅ | ✅ |

AgentRoom si sám dokumentuje, že agenti doporučující nájmy porušují. Claude Code
i Codex ale mají PreToolUse hooky se stejnou sémantikou (exit 2 = zablokovat),
takže se nájem dá vynutit **deterministicky na úrovni volání nástroje**. Jeden
skript obslouží oba klienty. Podrobně v [ADR 0001](docs/adr/0001-vynuceni-hookem.md).

Co tu schválně **není**: CRDT slučování souborů z AgentRoomu. Na to už máme git.

## Pravidla, na kterých to stojí

- **Nájem, ne zámek.** TTL 30 min. Spadlý agent nezablokuje repo napořád —
  nejhorší dopad je jedno TTL okno čekání.
- **Všechno, nebo nic.** Agent s půlkou cest stejně nemůže pracovat, ale ty půlky
  blokuje. Částečný úspěch je horší než čistý neúspěch.
- **Nájem na adresář pokrývá i soubory pod ním.** Nikdo dopředu nevyjmenuje
  všechno, čeho se dotkne.
- **Volný soubor si hook vezme sám.** Na explicitní `claim` se nedá zapomenout.
- **Fail-open.** Rozbitá koordinace nesmí zastavit práci — jinak ji první, co
  kdokoli udělá, je vypnout.

## Nástroje

| nástroj | k čemu |
|---|---|
| `claim(paths, purpose, ttl_seconds)` | rezervace dopředu — typicky adresář, než pustíš skript |
| `release(paths?)` | vrácení; bez argumentu vrátí všechno moje |
| `owner(path)` | kdo drží tuhle cestu (i přes nájem na adresář nad ní) |
| `room(status?)` | jedno volání na kontrolní bod: kdo je tu, co je zamčené, co je nového |
| `say(text)` / `inbox(since_id)` | vzkazy do místnosti a od kurzoru |
| `history(path?)` | kdo na co sáhl a jak to dopadlo — odpověď na „proč mě to zablokovalo" |

⚠️ **`say`/`inbox` není živý chat.** MCP je pull — protistrana si vzkaz přečte,
až se sama podívá. Na zámky a předávání to stačí, na konverzaci to bude působit
zpožděně. Proč to tak je: [ADR 0002](docs/adr/0002-sqlite-a-stdio.md).

Pro adresované zprávy s trvalým stavem doručení (task, ne jen broadcast) je
CLI `send` / `jobs` / `ack` / `wait` / `retry` / `cancel` — `agent-lease send
--help` pro tvar. `wait --for <agent>` blokuje bez tokenů modelu a hodí se
spustit na pozadí: harness probudí session, jakmile proces skončí.

## Live chat (fáze 2b)

Lokální loopback webová místnost nad stejnou databází — vidíš `say`/`send`
provoz, stav doručení, přítomnost i nájmy živě přes SSE, tmavé i světlé téma,
gradientové avatary agentů podle jména a živý odpočet TTL u nájmů. Neúspěšný
task lze zopakovat a nedokončený zrušit přímo v místnosti:

```bash
.venv/bin/agent-lease web         # funguje ihned i bez nové instalace entry pointu
# nebo po `uv sync`: agent-lease-webui
```

Token se vytváří atomicky rovnou na `0600` (`~/.agent-lease/webui.token`,
adresář `0700`) — ne `write_text` a dodatečný `chmod`, ať mezi tím soubor
chvíli neleží čitelný širší skupině — a přežije restart procesu. Identita
`michal` je vyhrazená pro tohle rozhraní — CLI/MCP odesílatel se jmenuje
jinak. Detaily a bezpečnostní hranice v
[AGENT-BRIDGE-DEEP-RESEARCH.md](docs/AGENT-BRIDGE-DEEP-RESEARCH.md).

## Co běží samo, bez uživatele u klávesnice

Nájmy vynucuje hook, ale samotné „podívej se, kdo tu je" by jinak zůstalo na
tom, že si o to agent řekne — a to je přesně ta půlka, která minule selhala.
Proto jsou i ostatní části na hoocích, ne na dobré vůli:

| událost | co se stane |
|---|---|
| **SessionStart** | do kontextu se vstříkne stav místnosti: kdo tu je, co drží, co ti vzkázal |
| **UserPromptSubmit** | doručí nové vzkazy — jednou, přes kurzor, ať se neopakují |
| **PreToolUse** | zablokuje editaci cizího rozpracovaného souboru |
| **PreToolUse (Bash)** | spouštěný skript si vezme do nájmu, takže ho druhý nesmí editovat za běhu |
| **Stop / SessionEnd** | vrátí všechny nájmy, jakmile agent dotáhne tah |

Ten předposlední řádek je ta konkrétní kombinace, která 9. 9. rozbila balíček:
jeden agent skript spouštěl, druhý ho zároveň editoval.

Poslední řádek je důvod, proč nájmy nemusí mít dlouhé TTL — po dokončení tahu se
uvolní samy. TTL zůstává jen jako pojistka pro tvrdý pád.

Injekce **mlčí, když není co říct.** Vstřikovat stav při každém promptu by byl
šum, agent by to začal přeskakovat a jsme zpátky u nástěnky, do které se nikdo
nedívá.

## Instalace

```bash
uv sync
```

Server (stdio; každý klient si spouští vlastní proces, sdílená je SQLite na disku):

```bash
# Claude Code
claude mcp add agent-lease -e AGENT_NAME=claude-code -- \
  ~/dev/projects/agent-lease-mcp/.venv/bin/agent-lease-mcp
```

```toml
# Codex — ~/.codex/config.toml
[mcp_servers.agent-lease]
command = "/home/buggy1111/dev/projects/agent-lease-mcp/.venv/bin/agent-lease-mcp"
env = { AGENT_NAME = "codex" }
```

Hook, který nájem vynutí — **bez něj je to jen slušně vychovaná nástěnka**:

```json
// ~/.claude/settings.json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write|NotebookEdit",
      "hooks": [{
        "type": "command",
        "command": "AGENT_NAME=claude-code ~/dev/projects/agent-lease-mcp/.venv/bin/agent-lease-guard"
      }]
    }]
  }
}
```

```json
// ~/.codex/hooks.json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "AGENT_NAME=codex /home/buggy1111/dev/projects/agent-lease-mcp/.venv/bin/agent-lease-guard"
      }]
    }]
  }
}
```

Zbytek automatizace (stejná struktura, jen jiné události a příkazy):

| událost | příkaz |
|---|---|
| `SessionStart`, `UserPromptSubmit` | `agent-lease-context` |
| `Stop`, `SessionEnd` | `agent-lease-release` |

Obě události berou stejný tvar konfigurace jako `PreToolUse`, jen bez `matcher`.
Codex i Claude Code je podporují shodně, včetně formátu
`hookSpecificOutput.additionalContext` pro vstřikování kontextu.

`AGENT_NAME` musí sedět mezi serverem a hookem, jinak si agent zablokuje vlastní
soubory. Když se v `room` objeví agent `unconfigured-*`, chybí právě tohle.

⚠️ **Codex potřebuje do sandboxu pustit adresář se stavem.** Píše se do
`~/.agent-lease/room.db`, což je mimo pracovní adresář, takže výchozí
`workspace-write` tam zápis zakáže — `claim` i `say` pak selžou a agent se do
místnosti vůbec nedostane. Jednorázově to řeší `codex --add-dir ~/.agent-lease`,
natrvalo tenhle blok:

```toml
# ~/.codex/config.toml
[sandbox_workspace_write]
writable_roots = ["/home/buggy1111/.agent-lease"]
```

Trvalá varianta není kosmetika: na `--add-dir` se dá zapomenout a projeví se to
tím, že koordinace tiše zmizí — přesně ten způsob selhání, kvůli kterému projekt
vznikl. (Novější Codex má i `[permissions.*]` profily; legacy blok výše zůstává
podporovaný a nekoliduje s ničím, dokud v konfiguraci není `default_permissions`.)

| proměnná | výchozí | k čemu |
|---|---|---|
| `AGENT_NAME` | `unconfigured-<host>-<pid>` | jméno v místnosti |
| `AGENT_LEASE_DB` | `~/.agent-lease/room.db` | kde je stav |
| `AGENT_LEASE_TTL` | `1800` | výchozí délka nájmu (s) |

## Struktura

Osm malých modulů, každý s jedním důvodem ke změně:

```
config.py     nastavení z prostředí, jedno místo pravdy o identitě agenta
models.py     slovník domény (Claim, Decision) — bez IO
policy.py     pravidla: co je zápis, co pokrývá jakou cestu, jak se rozhoduje
briefing.py   text vstřikovaný do kontextu — čisté funkce, testovatelné bez DB
store.py      SQLite a nic jiného
guard.py      PreToolUse hook: stdin → policy → exit kód (tenká slupka)
lifecycle.py  SessionStart / UserPromptSubmit / Stop hooky (tenká slupka)
server.py     MCP nástroje (tenká slupka)
```

Seam je mezi **pravidly** a **úložištěm**, protože měnit se budou pravidla
(přibude nástroj, upraví se pokrytí cest). Servisní vrstva mezi `server.py`
a `store.py` tu schválně není — bylo by to sedm funkcí na proklikávání.

## Známá omezení

- **Bash se hlídá jen částečně.** Zjistit z příkazové řádky, co všechno skript
  zapíše, nejde spolehlivě, a falešné blokování by bylo horší než žádné — agent
  by se naučil hook obcházet. Hlídá se proto jen jeden vzor, zato ten, který
  škodu způsobil: **spouštění skriptu** (`bash x.sh`, `./x.sh`, `python x.py`)
  si ten soubor vezme do nájmu. Na skripty, které zapisují jinam, je `claim`.
- **Jeden stroj.** Stav je soubor na disku.
- **Vzkazy nikoho nevyruší.**

## Vývoj

```bash
uv sync --extra dev
.venv/bin/python -m pytest tests -q     # 59 testů
.venv/bin/python -m ruff check src tests
```

MIT.
