# Fáze C — stav a uzavření, 12. 11. 2026

Ada, po nočním běhu s Noxem. Michal 12. 11.: Nox je několik dní bez limitu, fázi C dokončit a agent-lease tím na čas uzavřít. Navazuje na [návrh](PHASE_C_DESIGN_PROPOSAL.md), [C0](research/2026-11-12-c0-codex-worktree.md) a [oponenturu](research/2026-11-12-phase-c-oponentura-nox.md) (obojí na větvi `nox/c0-design-review-20261111`, review #18 schváleno).

## Co je hotové

| Balík | Větev @ commit | Review | Co dělá |
|---|---|---|---|
| B7 quality gate | `nox/b7-fixes-20261111` @ `0455529` | #12 schváleno | Sandboxovaný běh testů; nesloučeno, neaktivní |
| C1a úkol typu oprava | `ada/c1a-fix-tasks-20261111` @ `32cb343` | #13 schváleno | Operátorská politika + lidské schválení na jeden pokus; samo nic nezapisuje |
| Drobné opravy | `ada/small-fixes-20261112` @ `904b8fb` | #17 schváleno | `init` nesmaže rewake hook; web vysvětlí vypršelé přihlášení |
| C3a evidence spotřeby | `ada/c3a-usage-20261111` @ `479ebc8` | #15 opravy odevzdány, čeká na Noxe | Záznam každého pokusu (tokeny, cache, cena, skutečný model; neznámé = NULL), připnutý model |
| C3b stropy | `ada/c3b-budgets-20261112` @ `6fe8cc1` (nad C3a) | #16 opravy odevzdány, čeká na Noxe | Strop na úkol (přes všechny příjemce) a na agenta za 24 h; neznámá spotřeba při aktivním stropu zastaví |

Nic z toho není v `main` ani nasazené. Sloučení a nasazení schvaluje Michal; autor si vlastní práci neschvaluje, proto #15/#16 čekají na Noxe.

## Co je zablokované a proč

**C1b/C1c (agent zapisuje opravy ve worktree): NO-GO.** C0 změřil, že `codex exec --sandbox workspace-write` pustí zápis do `/tmp` mimo worktree a čtení mimo repo, a že agent nemůže commitovat bez zápisu do sdíleného `.git`. Před dalším krokem je potřeba:

1. Vlastní OS izolace zápisového běhu (stejný přístup jako B7: bwrap/mount namespace), ne jen přepínač klienta.
2. Rozhodnout git: samostatný klon s vlastními metadaty, nebo commit důvěryhodným procesem po skončení agenta.
3. Skutečná hranice lidské identity: agent dnes může číst `webui.token` i `room.db` (audit bod 6), takže „lidské schválení“ z C1a je proti zlovolnému agentovi jen procesní.
4. Nový C0 pokus na finálním obalu, pak review a souhlas Michala.

**C2 (smyčka oprav)** stojí na C1b/C1c a funkčních stropech C3.

## Provozní poznámky pro aktivaci C3

- Stropy jsou proměnné prostředí brokeru: `AGENT_LEASE_TASK_BUDGET_TOKENS`, `_TASK_BUDGET_USD`, `AGENT_LEASE_DAILY_BUDGET_TOKENS`, `_DAILY_BUDGET_USD` (0 = bez stropu).
- Codex cenu nehlásí. S USD stropem se jeho úkol po prvním pokusu zastaví; pro Codex nastavit tokenové stropy.
- Tokeny zahrnují cache (Claude `-p` načítá ~33k tokenů systémového promptu na každé volání; jednoslovná odpověď 0,13 USD).
- Zbývá z oponentury: ukládat požadovaný model odděleně od skutečně použitého (dnes jen skutečný).

## Doporučené pořadí

1. Michal: sloučit a nasadit schválené B7 + C1a + drobné opravy jedním oknem údržby (B7 zůstává neaktivní bez `quality-runner.toml`).
2. Až bude mít Nox limit: review #15/#16, pak C3 do stejného postupu a nastavit stropy.
3. C1b/C1c až po bodech 1–4 výše.
