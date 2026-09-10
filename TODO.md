# Stav ověření

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
