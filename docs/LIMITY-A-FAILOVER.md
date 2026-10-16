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

1. **Bridge** ho pozná sám (rate-limit ve výstupu / HTTP 429) → nahlásí limit,
   úkol odloží a zbytek fronty přesune.
2. **Interaktivní agent** má v pokynech: *„Blíží-li se ti limit nebo ti došel,
   zavolej `report_limit` (MCP) / `agent-lease limit --until +2h`."*
3. **Člověk** z terminálu: `agent-lease limit --agent codex --until +3h`.
   Zrušení: `agent-lease limit --agent codex --clear`.
4. Agent, který **přestal dávat známky života** (nedávný heartbeat chybí),
   se počítá jako nedostupný stejně.

## Co přesně se stane

- Přesouvají se jen `pending` tasky od nedostupného agenta, které čekají déle než
  `FAILOVER_GRACE`. **Rozdělaná práce zůstává u svého agenta** (zahájený task se
  nikdy nepřehazuje — mohl už něco změnit).
- Cíl = první agent řetězce, který žije a nemá limit. Nikdo takový → úkol zůstane
  čekat a zkusí se znovu při dalším průchodu.
- Odesílatel i člověk dostanou `↪ úkol #12 přesunut codex → claude-code (limit do 14:30)`.
- Probíhá to idempotentně v brokeru (každých ~15 s), v bridge i v hooku `context`,
  takže funguje i bez běžícího brokeru.
- `agent-lease limits` ukáže aktivní limity a řetězce; živý chat ukazuje agenta
  jako `rate-limited`.

## Co to neřeší

- Úkol přesunutý na slabší/odlišný nástroj může dopadnout jinak (bridge je navíc
  read-only). Proto vždy vidíš zprávu o přesunu a výsledek se ověřuje (viz JEV.md).
- Limit musí někdo nahlásit nebo ho bridge musí poznat; interaktivní Claude Code
  / Codex ho sám od sebe do místnosti neposílá.
