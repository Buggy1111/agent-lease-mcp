# Noční stav Nox → Ada / Michal

12. 11. 2026, přibližně 04:50 CET. Stav je vázaný na níže uvedené commity; pozdější změny vyžadují nové ověření.

| Oblast | Stav | Důkaz |
|---|---|---|
| B7/#12 | Adou schválené, Noxem nesloučené/neaktivované | `04555296d82db8d94b0131e852e64924ce2514c6`; schválený F2 `a854924`, 57 unit + 36/36 reálných E2E |
| C1a/#13 | APPROVED, security pass | `32cb3430ab68752a009365435987cd61e2e0fe52`; 79 testů, revoked-policy i legitimní start, Chromium skutečné UI, lint/scan/diffcheck |
| C3a/#15 | CHANGES_REQUESTED | `8c5eb1699e6344b35f60bf9bd589048169a9d878`; vlastní lease dovolí předem zapsat 0 a zamknout skutečný report; usage=None pokus nemá řádek |
| C3b/#16 | CHANGES_REQUESTED | `a8cfd237528e7b0718060ee2053d9c0495e60116`; unknown=0, Codex cache podruhé, STARTED bez recheck, task cap se resetuje failoverem |
| Drobné opravy/#17 | APPROVED, security pass | `904b8fb237cb1ee9b68c64bf9791e6c9344d9974`; 26 testů pass/5 skip, Chromium401 hláška přihlášení |
| C0 | NO-GO pro současné C1c | [Dvě syntetická měření](2026-11-12-c0-codex-worktree.md), výstup JSONL a usage doložené |
| Návrh C | Nutné úpravy | [Oponentura](2026-11-12-phase-c-oponentura-nox.md) |

Čtyři původní reprodukce C3 prošly assert kontrolou nad izolovaným exact snapshotem #16: první podvržený report, chybějící a NULL usage, dvojí cache, start po dosažení limitu. Pátý probe ověřil skutečný `failover_sweep`: úkol se 120 tokeny při task cap 100 získal po přesunu z codex na claude-code další lease. Existujících 17 testů usage prošlo; nedokrývají uvedené scénáře. Všechny čtyři review diffy byly nezávisle prohnány secret scannerem, bez nálezů.

Browser kontroly byly vůči lokálním syntetickým fixture, bez skutečných tokenů. #17 má pět skipů podle prostředí; produkční init ani hooks se při review nespouštěly. Dva první pokusy o browser ověření skončily kvůli chybám fixture (query v URL a selektor); po opravě fixture obě kontroly prošly. První příkaz s neexistujícím test souborem #13 nic neotestoval; správný výběr následně prošel 79/79.

Nox neprovedl push, merge do main, nasazení, úpravu služeb, hooků, konfigurace agentů ani změnu pauzy. Stav pauzy neslibujeme z historické zprávy: aktuální kontrola místnosti kolem 04:45 vracela `paused=null`. Další opravy C3 vlastní Ada, Nox provede nové review po předložení přesného commitu.
