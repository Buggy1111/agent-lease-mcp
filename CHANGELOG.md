# Changelog

## 0.2.0
- **Potvrzení příjmu:** automatické „viděl" (hook) a povinné „beru" (`accept`), připomínané při každém promptu; `overdue`.
- **Probuzení:** signální soubory, `wait` reaguje do ~0,2 s, `wait --rewake` pro asyncRewake hook.
- **Bridge:** autonomní worker (`codex exec`, `claude -p`, OpenRouter), hodinový strop, odklad při rate limitu, heartbeat lease.
- **Broker:** Unix socket 0600, jediný writer, `RemoteStore` s automatickým výběrem.
- **Doctor, prune, retence.**
- **Bezpečnost (security-findings.md):** DB 0600/adresář 0700, validace jmen a limity vstupů, redakce tajemství, politika odesílatelů tasků, lease token se nevypisuje, správa tasků jen vlastníkem/zadavatelem, atomický `edit_gate` místo check-then-claim, guard kryje `apply_patch` s více soubory i neznámé zapisovací nástroje, volitelný fail-closed, cizí text v kontextu označen jako nedůvěryhodná citace, web UI vyměňuje token za HttpOnly session cookie, limit SSE spojení.
- CI (3 verze Pythonu, ruff, pytest), Dependabot, pytest 9.

## 0.1.0
Nájmy, hooky, MCP nástroje, fronta tasků, live chat.
