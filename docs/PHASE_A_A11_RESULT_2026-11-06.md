# A11: restart WSL naostro (6. 11. 2026, 18:45–18:55)

Provedl Michal (`wsl --shutdown` z Windows, pak nový terminál WSL a Claude Code/Codex), ověřila Ada.
Runtime `runtime-d01bf3e`. Stav před restartem: `~/.local/share/agent-lease/a11-20261106-184529/`.

| Kontrola | Výsledek |
|---|---|
| Před restartem | 4 služby enabled + active, linger=yes, integrita ok, fronta 31 řádků (26 pending, 5 needs_review), nic `leased/started`, pauza vypnutá. Noxova rozpracovaná práce v UI worktree zazálohována. |
| Restart | `uptime` 1 min → WSL opravdu restartován. |
| Samostart služeb | broker, `bridge@claude-code`, `bridge@codex`, codex-wake **naběhly samy** v 18:49:55 z `runtime-d01bf3e` (`/proc/PID/exe`), oba bridge `Delegate=yes`. |
| Databáze a fronta | Integrita ok. Fronta shodná se snímkem, navíc jen #712 a #713 (chat zprávy poslané po snímku). Nic se samo nespustilo (0 `leased/started`), pauza se nezměnila. |
| MCP klienti | Nové `agent-lease-mcp` u Claude Code i Codexu z `runtime-d01bf3e`. |
| Živý test | `review request` s parametrem `repo` → review #1 (`awaiting_review` @ 7f4a64a, vzkaz Noxovi #714), `review check` → neschváleno. Codex bridge správně čeká (limit poskytovatele), nic nespustil. |
| `doctor` | Jen známá varování: `wsl_lifetime`, limit Codexu, 5 starých needs_review, Jev vypnutý. Selhal jen nesouvisející `pulseaudio` (systemd „degraded“). |

## Nálezy

1. **Web UI po restartu neběží.** Spouští se ručně jako přechodná služba `agent-lease-webui-deploy` (`systemd-run`). Po restartu znovu spuštěn ručně. Návrh: trvalá user unita `agent-lease-webui.service` (enabled, `Requires/After` broker), spouštěná z `current/venv/bin/agent-lease-webui`. Přihlášení tokenem zůstává.
2. **WSL se po přihlášení do Windows samo nespustí.** Ve Windows není naplánovaná úloha (`schtasks /Query /TN agent-lease` → nenalezena) a `deploy/windows/start-bridge.ps1` je neověřený. Služby tak naběhnou až po otevření terminálu WSL. Doporučení: ověřit `start-bridge.ps1` a s Michalovým souhlasem ho naplánovat při přihlášení.
3. Plný restart Windows (ne jen `wsl --shutdown`) neproveden. Chování po startu systému závisí na bodu 2.

**Verdikt:** restart WSL PASS (samostart, fronta, integrita, žádné samovolné spuštění). Body 1 a 2 jsou provozní dluhy, ne chyby jádra.
