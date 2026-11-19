# Fáze D – katalog schopností: Ada (Claude Code) a posouzení balíčků VojtaCode, 19. 11. 2026

**Stav:** jen čtení. Nic se neinstalovalo, žádný ZIP se nedostal mezi aktivní skills a žádná konfigurace se nezměnila. Archivy byly rozbaleny do izolovaného dočasného adresáře mimo `~/.claude`. Hodnoty konfigurace, tokeny a příkazy MCP serverů se nezapisují, jen názvy.

## 1. Stavy

- `installed` = je na disku nebo v konfiguraci;
- `enabled` = klient ho načítá;
- `verified` = dnes úspěšně použit v této relaci;
- `failed` = klient ho načítá, ale dnes se nepřipojil.

## 2. Ada – skutečné schopnosti

| Typ | Položka | Stav | Poznámka |
| --- | --- | --- | --- |
| MCP | agent-lease | verified | room, claim, review, verdict, wait |
| MCP | rea (Ghidra 12.1.4) | verified | RE akceptační test D3–D6 |
| MCP | shelly, ufal, ghidra, aidvertaiser, Claude Docs / Gmail / Calendar / Drive / Vercel (claude.ai) | enabled | v této relaci nevolány |
| MCP | chrome-devtools, gmail (lokální), sage | failed | timeout připojení |
| MCP | context7, glm, higgsfield, suno, puppeteer, macaly-cloud | installed | v této relaci jako nástroje nenačteny nebo jen autentizace |
| Plugin | hyperframes 0.9.9, ponytail 5.3.3, claude-mem 16.0.1, codex 1.0.6, socraticode 1.8.11 (failed), playwright (failed), voicemode (failed), trailofbits (insecure-defaults, supply-chain-risk-auditor, open-sourcing, modern-python, git-cleanup), vercel, frontend-design, document-skills, code-review, security-guidance | enabled | |
| Plugin | 13 dalších (legal, firebase, feature-dev, pr-review-toolkit …) | installed, vypnuté | |
| Skill (uživatel) | agent-design, code-audit, czech-document-anonymization, finish-the-job, hermes-cron-automation, inbox-autopilot, mes-module, owasp-security, pb-check, pr-description, profily-deploy-checklist, senior-architect, shelly-home-automation, translate-cz, webgl-mobile-crash-fix | enabled | owasp-security použit dnes |
| Hook | agent-lease (guard, context, Stop, SessionStart, SessionEnd), ponytail SessionStart, claude-mem | enabled | guard vynucuje edit-gate. Ostatní hooky jen doplňují kontext. |
| Vestavěné | Bash, Read/Edit/Write, Agent, Artifact, WebSearch/WebFetch, ToolSearch | enabled | |
| CLI | node 24, ffmpeg 6.1, gcc 13.3, bwrap, git, gh, uv | verified (PATH) | |

Mezi agenty se neplatí žádné pravidlo „co má Ada, má i Nox“. Katalog Noxe dělá Nox sám (`2026-11-19-d-capabilities-nox.md`).

## 3. Existující mechanismy, žádný nový katalog

`agent-lease self-audit` (D8) už čte konfiguraci hooků, MCP a skills a hlásí chybějící programy. Katalog proto **nevyžaduje nový systém**. Pokud ho Michal schválí, nejmenší krok je rozšířit D8 o přehled `installed/enabled` podle tabulky výše. Stav `verified` vzniká jen z běhu, ne z konfigurace, a záznam tedy musí nést čas posledního ověření. Doporučování nástrojů k úkolu je samostatná funkce (bridge prompt). Před ní je potřeba pilot, který změří režii v tokenech.

## 4. Balíčky VojtaCode a motion-rezie

| Balíček (Downloads) | SHA-256 (16) | Obsah | Zjištění |
| --- | --- | --- | --- |
| `VojtaCode-Claude-safe-use-balicek.zip` | `cb19aabeaa2d95ca` | skill `safe-use`, pravidlo `rules/safe-use.md`, `install.sh` a `.ps1`, příklad `settings` | Rozsah předem, výslovné „ano“ před nevratnou akcí, obsah obrazovky jsou data. **install.sh přesouvá existující `~/.claude/skills/safe-use` a `rules/safe-use.md` do `.bak`** a ptá se interaktivně. Tvrdou vrstvu tvoří jen `permissions.ask` v settings (návrh pro claude-in-chrome a windows-mcp, které Ada nemá). |
| `VojtaCode-balicek-claude-code.zip` | `c25e59f39491f52d` | checklist nastavení, preference, prompt na shrnutí | Návody pro začátečníky v Claude Desktop. Prompt na shrnutí (cíl, stav, rozhodnutí, další krok) se kryje s handoffem a checkpointem agent-lease. |
| `VojtaCode-balicek-konec-ai-odpadu.zip` (a stejný „(1)“) | `458146e942dfec25` | skill `hezky-web` a reference, anti-slop pravidla | **Archiv je poškozený:** `START-CLAUDE.md` má chybný CRC (obě kopie stejně). Skill vyžaduje 2–3 reference (Refero), design tokeny předem a kontrast ≥ 4,5:1. Překrývá se s pluginem `frontend-design` a skillem `artifact-design`. |
| `Skill-Prezentace-VojtaCode.zip` | `c56b30e0d44c1d64` | jeden SKILL.md a návod | Cesty v archivu se zpětnými lomítky (Windows). Překrývá se s `document-skills:pptx`, Slides artifact a `hyperframes:slideshow`. |
| `skill-image-vojtacode.zip` | `b7990d1c008ac7b5` | skill, skripty Node, **přibalené `node_modules` (Playwright, 19 MB)**, `.claude-flow` | **Riziko:** (1) přibalená, neověřená binární/JS závislost místo instalace z registru; (2) `.claude-flow/policy/state.json` obsahuje účtenky z autorova stroje (nástroj `ruflo`). Je to únik cizího stavu, ne tajemství. (3) Ovládá **přihlášený Google účet** přes Playwright s trvalým profilem (`~/.vojtacode-image/profil-prohlizece`) a scrapuje gemini.google.com, což je v šedé zóně podmínek služby. (4) `platforms: windows, macos`, Linux neuveden. |
| `motion-rezie.zip` | `e575fc2f8acdca1f` | textový skill | viz `2026-11-19-d13-motion-ada.md` |

## 5. UŽ MÁME / PŘIDAT / NEPŘIDÁVAT

| Kandidát (priorita v TODO) | Verdikt | Důvod |
| --- | --- | --- |
| Bezpečnost a důkazy dokončení (P1, safe-use) | **UŽ MÁME** (technicky), **PŘIDAT** jen princip | Rozsah, „ano“ před nevratnou akcí a stránku jako data už mají systémové instrukce Ady a auto-mode. Technicky vynucuje lease/edit-gate, bwrap a Quality Gate. Hodnotu má jedna věc: pro browser/computer use zapsat **rozsah předem**. Do Ady jen jako věta, ne jako skill. |
| Předání kontextu (P1, prompt-shrnuti) | **UŽ MÁME** | checkpoint, room, receipts, claude-mem a work_state. Při převzetí agent ověřuje živý stav. |
| Designový balík (P2, hezky-web / Refero / Open Design) | **INSPIROVAT SE**, neinstalovat teď | Překrývá se s frontend-design a artifact-design. Archiv je poškozený. Refero vyžaduje externí službu. |
| Vizuální kontrola (P2) | **UŽ MÁME** zčásti | `hyperframes snapshot`, `rea capture_web_screenshot`, Playwright (dnes `failed`). Jen v izolovaném prostředí. |
| Image + Prompt Master (P3) | **NEPŘIDÁVAT** | ovládá Google účet a scrapuje, přibaluje node_modules a cizí stav, chybí Linux. PROMPT-MASTER (8vrstvá metodika) lze číst jako inspiraci. |
| Prezentace (P3) | **NEPŘIDÁVAT** | duplicita s existujícími nástroji |
| Motion režie (D13) | **INSPIROVAT SE** | viz D13 |

## 6. Návrh pilotu (jen po Michalově souhlasu)

Tři scénáře z TODO (UI návrh, oprava bez grafiky, mediální podklad), každý s krátkým výběrem nástrojů z katalogu a bez něj. Měří se relevance, falešná doporučení, tokeny (`agent-lease efficiency`, D10), doba, bezpečnostní odmítnutí a úplnost důkazů. Ada navrhne, Nox nezávisle zkontroluje.
