# Předání práce Noxovi — agent-lease fáze A (30. 10. 2026)

**Od:** Ada (Claude Code) — končí jí týdenní limit. **Pro:** Nox (Codex), s plným zápisem.
**Zadání od Michala:** dokončit fázi A („hodiny bez dozoru, NIKDY tentýž úkol 2×“) a otestovat.
Michalova pravidla: nic nevratného ani produkčního bez jeho souhlasu, nové funkce (fáze B–D) až po schválení.

## Stav (HEAD `087aaa0`, 423 testů zelených)

Hotovo a zrevidováno tebou: dávky 1–3 + opravy tvých review #540, #545, #550, #555
(viz `git log c22d871..HEAD`, `docs/AGENT_LEASE_IMPROVEMENTS_TODO.md` → „Stav po fázi A“).
Nasazeno do služeb naposledy `1a814e0`; **`087aaa0` ještě NENASAZEN**.

Chaos test `tests/chaos/run_chaos.py` (izolovaná DB): 3min běh nad `087aaa0` PROŠEL
(žádný dvojí efekt, žádný sirotek, nic nevisí, karanténa držela, nic během pauzy).
30min běh se nepovedl (během běhu jsem přepsal testovací soubory) → **znovu spustit**.

## Co zbývá (v tomto pořadí)

1. **`uv run ruff check src tests`** hlásí 1 chybu → opravit.
2. **Review mých oprav tvého #555** (commit `087aaa0`): run_command metadata, accept s tokenem
   = started, interaktivní next_task = started (lease 3600 s), karanténa v `lease_next`/`accept`
   (`_deliveries._quarantined`), retry `needs_review` jen `actor=None` (web), PDEATHSIG.
   Zkontroluj i zbytek tvých bodů z #555, které jsem NEŘEŠIL:
   - workspace fingerprint: nečitelný untracked → `unknown`; index zvlášť od working diffu;
     identita worktree/root; symlinky/submoduly (`src/agent_lease_mcp/workspace.py`).
   - deterministické crashpointy (před/po started, před/po efektu, před/po COMMIT `complete`).
3. **Chaos test 30 min:** `.venv/bin/python tests/chaos/run_chaos.py --minutes 30 --workers 4`.
   Pokud najde chybu: oprava + regresní test + znovu.
4. **Nasazení** (vratné, lokální služby — Michal souhlasil s nasazováním opravené verze):
   `uv tool install --force --reinstall .` → `systemctl --user restart agent-lease-broker`
   → po 2 s `agent-lease-bridge@codex agent-lease-bridge@claude-code agent-lease-codex-wake`
   → `AGENT_NAME=codex agent-lease doctor`.
   ⚠️ Restart bridge ukončí běžící headless úkol — nasazuj, když nic neběží.
5. **Živý test po nasazení:** pošli si úkol, ověř `wait()`, `emergency_stop` + obnovení ve webu,
   karanténu (úkol s injekcí od agenta → needs_review), checkpoint.
6. **Dokumentace:** aktualizuj „Stav po fázi A“ v `docs/AGENT_LEASE_IMPROVEMENTS_TODO.md`
   a `CHANGELOG.md`; A11 (restart Windows) zůstává na Michalovi.
7. **Výsledek pošli Michalovi** (`say`) krátce česky: hotovo / co zbývá / verdikt fáze A.

## Známé omezení (nevyřešitelné kódem)
Agent se shellem pod stejným OS uživatelem může číst web token i zapisovat přímo do SQLite —
skutečná hranice je jen oddělení OS uživatele/sandbox (shodli jsme se v #545/#550).

## Pravidla repa
Commit per krok, česky, `Co-Authored-By: Codex <noreply@openai.com>`; před commitem
`uv run ruff check src tests` + `uv run pytest -q`. Nemazat cizí soubory, nepushovat.
