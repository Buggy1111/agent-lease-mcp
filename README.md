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

⚠️ **Není to živý chat.** MCP je pull — protistrana si vzkaz přečte, až se sama
podívá. Na zámky a předávání to stačí, na konverzaci to bude působit zpožděně.
Proč to tak je: [ADR 0002](docs/adr/0002-sqlite-a-stdio.md).

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

`AGENT_NAME` musí sedět mezi serverem a hookem, jinak si agent zablokuje vlastní
soubory. Když se v `room` objeví agent `unconfigured-*`, chybí právě tohle.

| proměnná | výchozí | k čemu |
|---|---|---|
| `AGENT_NAME` | `unconfigured-<host>-<pid>` | jméno v místnosti |
| `AGENT_LEASE_DB` | `~/.agent-lease/room.db` | kde je stav |
| `AGENT_LEASE_TTL` | `1800` | výchozí délka nájmu (s) |

## Struktura

Šest malých modulů, každý s jedním důvodem ke změně:

```
config.py   nastavení z prostředí, jedno místo pravdy o identitě agenta
models.py   slovník domény (Claim, Decision) — bez IO
policy.py   pravidla: co je zápis, co pokrývá jakou cestu, jak se rozhoduje
store.py    SQLite a nic jiného
guard.py    hook: stdin → policy → exit kód (tenká slupka)
server.py   MCP nástroje (tenká slupka)
```

Seam je mezi **pravidly** a **úložištěm**, protože měnit se budou pravidla
(přibude nástroj, upraví se pokrytí cest). Servisní vrstva mezi `server.py`
a `store.py` tu schválně není — bylo by to sedm funkcí na proklikávání.

## Známá omezení

- **Bash se nehlídá.** Z příkazové řádky nejde spolehlivě zjistit, co skript
  zapíše, a falešné blokování by bylo horší než žádné (agent by se naučil hook
  obcházet). Na skripty sahající na sdílené věci je explicitní `claim`.
- **Jeden stroj.** Stav je soubor na disku.
- **Vzkazy nikoho nevyruší.**

## Vývoj

```bash
uv sync --extra dev
.venv/bin/python -m pytest tests -q     # 40 testů
.venv/bin/python -m ruff check src tests
```

MIT.
