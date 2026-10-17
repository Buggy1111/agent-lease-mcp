# Limity agentů a failover

Problém: agentovi dojde limit (týdenní kvóta, 5h okno) uprostřed práce a jeho
fronta stojí, dokud se neresetuje. Řešení: **nezahájené úkoly se přesunou na
dalšího agenta v řetězci**, a všichni o tom vědí.

## Nastavení

```bash
# kdo zaskočí za koho (pořadí = priorita; vše přes nakonfigurované bridge/agenty)
export AGENT_LEASE_FALLBACKS="codex=claude-code,openrouter;claude-code=codex,openrouter"
export AGENT_LEASE_FAILOVER_GRACE=60   # úkol musí čekat aspoň 60 s, ať krátký výpadek neskáče
```

`openrouter` je bridge s `--provider openrouter` (viz README) — obvykle poslední
záchrana, protože nemá kvótu předplatného a běží jen jako textový dotaz.

## Jak se o limitu dozvíme

1. **Codex bridge** čte typovaný `ordinaryUsageAllowed` z lokálního Codex App
   Serveru (`account/rateLimits/read`) před převzetím práce. `null` znamená
   `unknown`, nikoli obnovení. Pro jednorázovou kontrolu lze použít
   `agent-lease provider-sync --agent codex`.
2. **Claude Code / obecný CLI bridge** uloží `limited` až po skutečné chybě
   procesu. Čas resetu uloží jen tehdy, když jej poskytovatel opravdu sdělil;
   jinak má provider stav `resets_at = null` a task pouze označený lokální
   backoff. Úspěšný kontrolní běh pak potvrdí návrat do `available`.
3. **OpenRouter bridge** používá HTTP 402/429 a případný `Retry-After`.
4. **Interaktivní agent** má v pokynech: *„Blíží-li se ti limit nebo ti došel,
   zavolej `report_limit` (MCP) / `agent-lease limit --until +2h`."*
5. Agent, který **přestal dávat známky života** (nedávný heartbeat chybí),
   se počítá jako nedostupný stejně.

Manuální limit smí identita hlásit jen sama za sebe. Například pro Codex:

```bash
AGENT_NAME=codex agent-lease limit --until +3h --reason "confirmed by user"
AGENT_NAME=codex agent-lease limit --clear
```

Použité veřejné kontrakty: [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
a [Anthropic API errors](https://docs.anthropic.com/en/api/errors). Codex kontrakt
se ověřuje proti schématu nainstalované verze pomocí
`codex app-server generate-json-schema`; agent-lease nepoužívá privátní socket.

## Co přesně se stane

- Přesouvají se jen `pending` tasky od nedostupného agenta, které čekají déle než
  `FAILOVER_GRACE`. **Rozdělaná práce zůstává u svého agenta** (zahájený task se
  nikdy nepřehazuje — mohl už něco změnit).
- Cíl = první agent řetězce, který žije a má dostupného poskytovatele. Stav
  `unknown` se jako cíl nepoužije. Nikdo takový → úkol zůstane
  čekat a zkusí se znovu při dalším průchodu.
- Odesílatel i člověk dostanou `↪ úkol #12 přesunut codex → claude-code (limit do 14:30)`.
- Probíhá to idempotentně v brokeru (každých ~15 s), v bridge i v hooku `context`,
  takže funguje i bez běžícího brokeru.
- `agent-lease limits` ukáže aktivní limity a řetězce; živý chat ukazuje agenta
  jako `rate-limited`.

## Co to neřeší

- Úkol přesunutý na slabší/odlišný nástroj může dopadnout jinak (bridge je navíc
  read-only). Proto vždy vidíš zprávu o přesunu a výsledek se ověřuje (viz JEV.md).
- Interaktivní session sama místnost neobslouží, pokud není aktivní hook/turn;
  bez supervised bridge zůstává doručený task uložený, ale neprobudí model.
- Claude Code nemá v dokumentovaném CLI samostatný dotaz na stav předplatitelské
  kvóty. Agent-lease proto žádnou dostupnost nepředstírá před reálným úspěchem.
