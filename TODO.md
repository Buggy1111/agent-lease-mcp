# Agent Lease MCP – TODO

Stav k 21. 10. 2026 ráno (ověřeno ostrými testy proti běžícím službám).
✅ hotovo · 🟡 z větší části · ⬜ otevřené

- ✅ 1. **Společná místnost** — Ada, Nox i workery vidí ostatní, stav a aktivitu.
- ✅ 2. **Automatické předávání zpráv** — adresované zprávy a úkoly mezi agenty.
- ✅ 3. **Potvrzování úkolů** — viděl → beru → hotovo, opakování, zrušení.
- ✅ 4. **Automatické probuzení agentů** — naostro 20.–21. 10. obousměrně:
  Adu budí `asyncRewake` hook (Stop + SessionStart), Noxe služba
  `agent-lease-codex-wake` přes `codex queue` do pojmenované session. Po skončení
  limitu Noxe budík sám pošťouchne k rozdělané práci (jednou za epizodu).
- 🟡 5. **Automatické spuštění práce** — ✅ naostro: převezme, potvrdí, spustí
  `codex exec` read-only a vrátí čistou odpověď (opraveno: dřív celý přepis běhu).
  Zbývá: zápisový režim (se schválením) a průběžné hlášení dlouhé práce.
- 🟡 6. **Hlídání limitů agentů** — týdenní limit Claude, typovaný stav Codexu,
  oddělení interaktivní session od workeru, žádné 15minutové retry bouře.
- ✅ 7. **Pokračování po obnovení limitů** — naostro 20. 10. (#300): během limitu
  úkol čekal, po zrušení proběhl přesně jednou (1 pokus, 1 výsledek). Test odhalil
  a opravil chybu: ohlášený limit worker ignoroval, když poskytovatel hlásil
  „available" (#296). Failover na jiného agenta zatím vypnutý (`AGENT_LEASE_FALLBACKS`).
- ✅ 8. **Bezpečné přidělování souborů** — vynucené hookem (Edit/Write/apply_patch,
  spouštěné skripty, zjevné zápisy z Bashe), atomický `edit_gate`.
- ✅ 9. **Automatické uvolňování zámků** — Stop/SessionEnd vrací nájmy, TTL pojistka.
- ✅ 10. **Obnova po pádu agenta** — stav úkolu se drží, `needs_review`, žádné dvojí provedení.
- 🟡 11. **Bridge pro skutečné AI agenty** — ✅ `codex exec` naostro (3 úkoly);
  `claude -p` čeká na konec limitu Ady; OpenRouter jen v testech.
- ✅ 12. **Automatické testování výsledků** — plná sada pytest + ruff, CI na 3.11–3.13.
- ✅ 13. **Vzájemné kontroly Ada ↔ Nox** — 20.–21. 10. běžely bez Michala:
  Nox našel 6 nálezů (2 BLOCKER: dvojí provedení úkolu, souběh migrace), Ada 3
  (mj. obejití filtru citací); každý opraven, přetestován a potvrzen druhým.
  Pravidla autonomie Noxe: `~/.codex/AGENTS.md`.
- ✅ 14. **Bezpečnostní audit** — 13 nálezů opraveno/zdokumentováno a zveřejněno,
  branch protection, Dependabot, private vulnerability reporting, Jev guard.
- 🟡 15. **Windows 11 + WSL autostart** — broker a oba workery běží jako systemd
  služby ve WSL, `deploy/windows/start-bridge.ps1` existuje. Opraveno: první
  kontrola limitu po bootu se přeskakovala (CI). Zbývá: Michal restartuje PC →
  `agent-lease doctor`.
- ⬜ 16. **Dlouhodobé ostré testování** — oba agenti na bezpečném projektu,
  kolize, limity, restarty, chyby.
- ⬜ 17. **Třetí agent – koordinátor** — rozděluje práci, hlídá agenty, testy, limity.

## Nejbližší kroky

1. Michal: restart PC → `agent-lease doctor` (bod 15).
2. Ostrý test `claude -p` workeru (bod 11); limit se teď v místnosti po resetu
   ukazuje jako „recovering“, worker ho zkusí s dalším úkolem.
3. Zapnout failover `AGENT_LEASE_FALLBACKS` a otestovat přesun úkolu (bod 7, část 2).
4. Zapnout Jev (`AGENT_LEASE_JEV=1` + klíč) — bez něj je jedinou branou heuristika.

## Historie

# Stav ověření

## ✅ Aktualizace 11. 9. 2026 — live chat MVP

Společná práce Claude Code a Codexu doplnila a ověřila:

- adresované `chat`/`task` zprávy a trvalé stavy doručení;
- atomické převzetí tasku, lease token, retry, cancel, deduplikaci a crash stav
  `needs_review`;
- `agent-lease wait`, který naživo probudil nečinnou Claude session bez
  modelových tokenů;
- bezpečný loopback webový chat přes SSE, snapshot posledních 200 zpráv,
  reconnect bez viditelných duplicit, přítomnost, nájmy a ovládání tasků;
- privátní token `0600`, kontrolu Origin, zákaz poslouchat na `0.0.0.0` a
  rezervaci identity `michal` pouze pro autentizované UI;
- 85 testů a lint. Tři skutečné socketové testy se v Codex sandboxu přeskakují,
  protože zakazuje i loopback bind; stejné HTTP handlery jsou proto testované
  přímo bez socketu.

### ✅ Doladění 11. 9. 2026 večer — na přímé Michalovo zadání

- **Permission race opravena:** token se vytváří atomicky rovnou na `0600`
  (`os.open(O_CREAT|O_EXCL)`, adresář `0700`), ne `write_text` + dodatečný
  `chmod` — nalezeno automatickým security review, ověřeno na reálném stroji
  (`~/.agent-lease` bylo `755`, teď `700`).
- **UI redesign:** gradientové avatary agentů podle jména, pulsující stav
  online/offline, pill badge na `kind`/`state`, živý TTL odpočet u nájmů,
  glass-card vzhled, tmavé i světlé téma (`prefers-color-scheme`), mobilní
  layout pod 720px. HTTP kontrakt a JS logika beze změny — jen render vrstva.
- Ověřeno vizuálně (Chrome, desktop 1400×900, mobil 390×844, obě témata) a
  naostro nad reálnou databází místnosti, ne jen testovací.

Spuštění bez reinstalace entry pointu:

```bash
.venv/bin/agent-lease web
```

Plný autonomní most ještě není hotový: broker jako jediný SQLite writer,
Codex App Server adaptér, trvalý Claude `--bg` adaptér a Windows bootstrap jsou
samostatné následující fáze z `docs/AGENT-BRIDGE-DEEP-RESEARCH.md`.

Ověřeno **naživo, oběma agenty** 10. 9. 2026. Body 1–5 padají, zbývá rozhodnutí
v bodu 6 a dvě věci na Michalovi.

## ✅ 1. Vidíme se navzájem
Oba agenti se v `room()` vidí jako aktivní peer. Codex jede přes CLI, Claude Code
přes MCP nástroje, obojí sahá na tutéž SQLite.

## ✅ 2. Kontext se vstřikuje při startu
Claude Code dostal při startu session řádek `[agent-lease] Jsi v místnosti jako
claude-code…` bez jakéhokoli zásahu.

## ✅ 3. Vzkazy se doručují
Codex potvrdil, že vidí zadání i vzkazy, aniž by se do místnosti sám podíval —
dostal je vstříknuté hookem. ⚠️ Doručení přijde až s **dalším promptem** agenta;
dokud druhý agent nemá tah, vzkaz čeká. Při tomhle ověřování čekal jeden vzkaz
přes 4 hodiny — session stála na vyčerpaném limitu. Není to chyba doručování,
ale ukazuje to strop: na vzkaz se nedá spoléhat jako na upozornění, protože
protistrana nemusí mít tah celé hodiny.

## ✅ 4. Nájmy se uvolňují na konci tahu
Nájem vzatý v jednom tahu byl v následujícím volný (`held_by: null`) — Stop hook
ho vrátil sám, bez čekání na TTL.

## ✅ 5. Ostrá zkouška kolize z 9. 9.
**Prošlo.** Codex si vzal `deploy/` a pustil `make-work-package.sh` (PID 528561,
schválně do `/tmp`, ne na flashku). Za jeho běhu zkusil Claude Code editovat ten
samý skript a hook ho odmítl:

```
[agent-lease] make-work-package.sh má rozpracovaný codex
(Ostrý test: běží make-work-package.sh; …), nájem vyprší za 1752 s.
Nesahej na něj. Vezmi si jinou práci, nebo se ozvi přes `say`.
```

Soubor zůstal beze změny (ověřeno `md5sum` i `git status`), odmítnutí je
v `history()`. To je celý důvod existence projektu, a poprvé je doložený.

### Co ta zkouška odhalila (obojí opraveno)
- **Agent bez MCP mizel z místnosti.** Presence se aktualizovala jen při
  SessionStart, takže Codex po 15 minutách práce vypadal jako neaktivní.
  Hooky i CLI teď hlásí presence a `status` se nepřepisuje jménem posledního
  volání.
- **Codex se do místnosti nedostal vůbec**, dokud sandbox nepustil
  `~/.agent-lease`. Viz README, sekce o `writable_roots`.

## 6. Rozhodnout: PyPI, nebo lokální?
Michal už na PyPI má `shelly-mcp` i `anonymize-mcp`. Pro publikaci mluví, že
prior art je slabý a **hookové vynucení nemá nikdo jiný** (viz tabulka v README).
Proti mluví, že celé je to zatím ověřené na jednom stroji a dvou klientech.

## Zbývá na Michalovi
1. **Zapsat `writable_roots` do `~/.codex/config.toml`** (blok je v README).
   Bez toho bude Codex po každém startu bez `--add-dir` mimo místnost.
2. **Zmergovat větev `auto/presence-a-sandbox`** do `master`.

## ✅ 22. 9. 2026 — Jev Bash safety-gate (samostatný, volitelný)

Nový modul (`jev_policy.py`/`jev_client.py`/`jev_guard.py`), vlastní entry
point `agent-lease-jev-guard`, zapojený vedle lease guardu, ne uvnitř něj —
proč, je v [ADR 0003](docs/adr/0003-jev-samostatny-guard.md). 124 testů a
lint zelené, ruční E2E na skutečné binárce ověřený.

Zbývá na Michalovi, než z toho bude reálná ochrana:
1. **Přidat `PreToolUse` blok pro `agent-lease-jev-guard` do
   `~/.claude/settings.json`** (přesný JSON v README) — bez tohohle běží jen
   testy, v Auto Mode nechrání nic.
2. Natvrdo napsané vzory (`rm -rf /`, force push, …) budou aktivní hned po
   zapojení, bez API klíče. Jev samotný ne — je vypnutý (`JEV_GUARD_ENABLED`).
3. **Rozhodnout, jestli/kdy pořídit `TYPESAFE_API_KEY`** a zapnout
   `JEV_GUARD_ENABLED=true` — napřed jen v `shadow` režimu (výchozí),
   nechat běžet týden, podívat se do `~/.agent-lease/jev-guard.log`, teprve
   pak `JEV_GUARD_MODE=enforce`.
4. Codex-side zapojení zatím schválně chybí — jméno jeho shell nástroje
   v hook payloadu není ověřené, hádat by bylo horší než nezapojit.
