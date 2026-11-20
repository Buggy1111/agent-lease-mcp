# Aktivace B7 + C + D3–D7 a nasazení `cbf6e1e` — 20. 11. 2026, Ada

Michal 20. 11.: „udělej těch 5 úkolů co tam visí, pak zapni kontrolu kvality, automatické opravy
a výzkumný tým“. Kód: review #39 (odemknutí autonomního režimu), #41 (conftest) a #40 (bwrap probe)
schválil Nox, security pass.

## Fronta

Pět starých `needs_review` pro claude-code (#594, #603, #605, #617, #623 z 5.–6. 11.) bylo vyřízeno
jinde (oprava run-fence `b483265`, review PASS `PHASE_A_RUN_FENCE_REVIEW_2026-11-05.md`, report #637).
Zrušeny (`cancel`, ne `retry` — zastaralá zadání se znovu nespouští). Nox stejně uzavřel svých pět.

## D3–D7 výzkumný tým

- `agent-lease research-probe --agent claude-code`: **PASS** (run_ok, control_read_by_tool,
  watch_saw_control, canary_attempt_denied, canary_not_opened, canary_not_in_output) →
  `~/.agent-lease/research-probe.json`.
- `~/.agent-lease/research.toml` (0600): `enabled=true`, `agents=["claude-code"]`, `budget_tokens=400000`,
  `max_parts=4`, `concurrent=2`, `deadline_minutes=60`, `synth_minutes=30`. Codex pro výzkum vypnutý
  (rozhodnutí 19. 11.: do doložení hranice čtení).

## C automatické opravy

- `MODE_LOCKS = {}` (review #39). Režim přepíná dál jen člověk z webu; výchozí zůstává `intelligent`.
- `~/.agent-lease/fix-runner.toml` (0600): agenti `codex`, `claude-code`; repozitář `agent-lease-mcp`;
  ověřovací příkaz `.venv/bin/python -m pytest -q`.
- **Zbývá Michal:** ve webu (`agent-lease web --open`) přepnout režim na Autonomní; každou jednotlivou
  zápisovou opravu povolit tlačítkem „Povolit zápis jednou“.

## B7 Quality Gate

- Neměnný strom nástrojů `~/.local/share/agent-lease/quality-tools-20261120` (python + site, read-only),
  zkopírovaný z ověřeného `agent-lease-b7-toolchain` bez zastaralé kopie balíčku; závislosti shodné
  s produkčním runtime. Piny: python `55bc8b49…`, site `382d827e…`, exe `0195e34a…`.
- Testové opravy: podprocesy testů najdou `src` bez `pip install -e` (#41); `test_fix_runner` se
  přeskočí jen tam, kde `bwrap` reálně nevytvoří sandbox — v B7 je vnořený (#40). Těchto 38 testů
  izolace oprav běží lokálně a v CI, v B7 se nepočítají jako PASS.
- Broker drop-in `20-quality-limits.conf`: `MemoryMax=2G`, `CPUQuota=100%`, `TasksMax=256` (ověřeno
  `systemctl show`).
- `~/.agent-lease/quality-runner.toml` (0600), repozitář `agent-lease-mcp`, `timeout_seconds=600`.
  Nejdřív `automatic=false`: ostrá kontrola přes produkční broker na `cbf6e1e` — tests/lint/build
  **PASS**, security **REVIEW** (slučovací commit nemá vlastní B6 schválení — správně), 3 min.
  Pak `automatic=true` → Stop/release hooky obou klientů zařazují čistý HEAD.
- GitHub branch protection / required checks **nejsou** zapojené: dashboard FAIL ruční merge na GitHubu
  nezablokuje.

## Nasazení `cbf6e1e`

| Krok | Výsledek |
| --- | --- |
| Runtime | `runtime-cbf6e1e`, interpret z `runtime-79fe207`, závislosti připnuté — shodné, `pip check` OK |
| Artefakt | wheel SHA-256 `865113550621e16c5ef7a4364af535d798d2b3b14ee519dfe349fe7e23851ef0`, 54/54 souborů shodných se zdrojem; proti živému změněn jen `_deliveries.py` |
| Okno | 0 `leased/started`, služby zastaveny, DB bez držitele |
| Záloha | `~/.local/share/agent-lease/deploy-backup-20261120-ada-cbf6e1e` (DB integrita `ok`, fronta, unit soubory, `previous-current` = `runtime-79fe207`) |
| Ověření | 4 služby active na `runtime-cbf6e1e`, limity brokeru platí, integrita `ok`, 138/138 nedokončených doručenek shodných, `doctor` „Looks good“ (jen WSL a Jev) |

**Rollback:** `automatic=false` (nebo smazat `quality-runner.toml` / `fix-runner.toml` / `research.toml`),
ve webu režim zpět na Inteligentní; kód: zastavit služby, `current` zpět na `runtime-79fe207`, odebrat
`20-quality-limits.conf`, `daemon-reload`, spustit, ověřit.

## Ostré ověření po aktivaci

- **B7 automaticky:** Stop hook Ady ve 14:24 sám zařadil `ad2eaa8`; běh dokončen 14:28 —
  tests/lint/build **PASS**, security bez B6 schválení (neprošlé review, správně nezelené).
- **Výzkum (D3):** výzkum #1 (2 části + syntéza) proběhl celý, spotřeba 105 237 tokenů započítána proti
  mezi 400 000. **Známé omezení:** výzkumníci běží nad prázdným adresářem — přístup ke kopii
  repozitáře jen pro čtení, se kterým počítá návrh (§ 7), není implementovaný; web je vypnutý. Výzkum
  tedy dnes odpovídá jen ze znalostí modelu, ne z kódu. Doplnění repozitáře = samostatná změna s review.
- **Full pytest** na čistém exportu `cbf6e1e`: 953 PASS / 30 SKIP / 1 XFAIL + 37 setup errors
  `test_fix_runner` (AF_UNIX path too long v izolovaném HOME, stejný jev jako u Noxe 19. 11.); tentýž soubor
  nad stejným kódem v běžném prostředí 38 PASS. Ruff PASS.
