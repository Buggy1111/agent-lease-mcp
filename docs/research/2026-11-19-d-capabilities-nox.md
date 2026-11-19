# Fáze D – katalog schopností: Nox (Codex), 19. 11. 2026

Stav: text dodal Nox (headless codex-bridge, úkol #1590, zpráva #1610); do souboru ho beze změny obsahu přenesla Ada. Běh #1590 pouze četl. Žádná instalace, změna konfigurace, render ani pilot. Platí pro headless codex-bridge relaci, nikoli automaticky pro interaktivní Codex, Adu nebo jiný profil.

## Význam stavů a důkazy

- installed: nalezené soubory balíčku; samotná cache/distribuční kopie nedokládá instalaci do aktivního profilu.
- enabled: host v této relaci nabízí skill nebo nástroj; u MCP to ještě nedokládá spojení, oprávnění účtu ani funkčnost každé operace.
- verified: uvedená konkrétní operace dnes úspěšně proběhla.
- unavailable: pro tuto relaci nenalezený aktivní nástroj; nejde o důkaz nepřítomnosti na celém počítači.
- unknown: bez čtení soukromé konfigurace nelze určit.

Čas evidence: 19. 11. 2026 přibližně 20:05–20:16 Europe/Prague. Zdroje: hostem dodaný katalog skills a ALL_TOOLS, existence katalogových adresářů pluginů, veřejné manifesty balíčků, command -v a lokální --version. Soukromé config.toml, hooks.json, .env, tokeny ani účty se nečetly. Přítomnost v PATH se eviduje samostatně od funkčního testu.

## Skills

Níže enabled znamená nabídnutý hostem v této relaci, nikoli automaticky bezpečný ke spuštění ani end-to-end otestovaný. Existenci balíčků dokládá adresářová inventura cache; u systémových skills také přečtený openai-docs.

| Rodina / verze | Nabídnuté schopnosti | Stav a omezení |
| --- | --- | --- |
| systémové | imagegen, openai-docs, skill-creator, skill-installer | enabled; openai-docs přečten; generování ani instalace netestována |
| claude-mem 15.10.0 | cost report, babysit, backup, build/release, ccs-align, childhood, cloud-sync, design-is, do, handoff, how-it-works, knowledge-agent, learn-codebase, make-plan, mem-search, mode-creator, oh-my-issues, owner-goals, pathfinder, request-progress, screenpipe, smart-explore, standup, timeline-report, version-bump, weekly-digests, what-the, wowerpoint | installed/enabled; v tomto běhu bez paměťových dotazů a bez zápisů |
| ponytail 5.3.3 | ponytail, audit, debt, gain, help, review | installed/enabled; full je aktivní instrukce, review přečten; nejde o automatický test kódu |
| codex-security 0.1.33 | assess-patch-risk, attack-path-analysis, check-skills, cloud-findings, deep-security-scan, define-security-policy, finding-discovery, fix-finding, propose-security-hardening, publish-findings, resolve-dependency-alert, security-diff-scan, security-scan, threat-model, threat-model-visualize, track-findings, triage-finding, validation, verify-fix, vulnerability-writeup | installed/enabled; check-skills přečten pro omezenou kontrolu podkladů; plný bezpečnostní audit ZIPů neproveden |
| ads-manager 0.1.40 | account-admin, ad-creation, business-agent-setup, delivery-recovery, entity-management, help, insights, onboarding, review, setup, start-agent, starter-campaign, draft-business-agent | installed/enabled; žádné reklamní účty ani operace netestovány |
| sites 1.1.7 | sites, sites-remix | installed/enabled; bez tvorby webu a publikování |
| work-pets 0.1.6 | create-pet, pets, update-pet | installed/enabled; bez volání |
| youtube-agent 0.1.0 | yt-audit, chapters, comment, edit, package, plan, retention, script, seo, shorts, viral | installed/enabled; bez volání |
| visualize 1.0.89 | visualize | installed/enabled; bez vykreslení |
| write-like-me 0.1.1 | write-like-me | installed/enabled; bez načítání osobních vzorků |
| plugin-management 0.1.0 | plugin-management | installed/enabled; bez změn pluginů |

Na disku jsou i jiné a starší balíčky (např. build-web-apps, starší codex-security, openai-templates). V této relaci nejsou uvedeny mezi aktivními skills; existence cache se nesmí převést na enabled. Uživatelský adresář .codex/skills při prostém výpisu neměl viditelné položky; nejde o inventuru všech profilů.

## MCP a vestavěné nástroje

| Typ / položka | Účel | Stav / důkaz | Omezení a fallback |
| --- | --- | --- | --- |
| agent-lease MCP | místnost, zprávy, review, vlastnictví úkolů | enabled; verified: inbox, accept, review(show), room, send | ověřeny jen uvedené operace; CLI a MCP mohou používat různé identity, preferovat správnou relaci MCP |
| claude-mem MCP (mcp_search) | paměť, struktura kódu, pracovní stav | enabled podle ALL_TOOLS | spojení a obsah neověřeny; fallback lokální čtení zdrojů |
| codex-security MCP | bezpečnostní workbench | enabled podle ALL_TOOLS | backend neověřen; fallback omezená statická kontrola |
| REA MCP | reverse engineering, inspekce artefaktů a browseru | enabled podle ALL_TOOLS | Ghidra, zařízení ani browser v této relaci neověřeny; Adin D3–D6 test se na Noxe nepřenáší |
| Shelly MCP | domácí zařízení | enabled podle ALL_TOOLS | zařízení se nečetla ani neovládala; není potřeba pro D13 |
| Apps konektory | Atlassian, GitHub, Gmail, Google Calendar, Linear, Ads Manager, Document Control, Pets, Sites, Writing Style, Plugin Management, Safety Settings, Hotline | enabled ve smyslu nabízených nástrojů | účty, autentizace a oprávnění neověřeny; žádná data účtů se nečetla |
| Macaly Cloud | tvorba a hosting webů | enabled podle ALL_TOOLS | nevoláno, žádný přenos ani deploy |
| lokální shell/orchestrace | čtení souborů, git diff, lokální výpočty | verified: exec_command, write_stdin, functions.exec | filesystem tohoto běhu read-only; přítomnost apply_patch není povolení zapisovat |
| obraz / web | view_image, imagegen, web | enabled podle hosta | nevoláno; imagegen a web mohou přenášet data, nejsou součástí této inventury |
| spolupráce / vstup | collaboration, request_user_input_async, clock | enabled podle hosta | subagenti nespouštěni; jejich dostupnost sama neuděluje oprávnění delegovat |

Hooky: agent-lease a claude-mem dodaly kontext relace. Přesný seznam aktivních hooků, jejich konfigurace a účinnost edit-gate nejsou bez čtení soukromého nastavení ověřeny. Read-only sandbox je doložen nastavením tohoto běhu, nikoli destruktivním testem zápisu.

## CLI a runtime

| Položka | Evidence | Závěr |
| --- | --- | --- |
| Node | node --version → v18.19.1 | verified pouze verze; nesplňuje HyperFrames CLI 0.9.9 požadující >=22 |
| FFmpeg | ffmpeg -version → 6.1.1-3ubuntu5 | verified pouze verze, kódování netestováno |
| git | git --version → 2.43.0; diff/worktree/status čteny | verified uvedené čtecí operace |
| uv | uv --version → 0.11.12 | verified pouze verze |
| bwrap | bwrap --version → 0.9.0 | verified pouze verze, izolace v tomto úkolu netestována |
| Python 3 | úspěšný in-memory výpočet hashů/CRC a secret scan | verified konkrétní čtecí skripty, bez extrakce ZIPů a bez bytecode zápisů |
| codex, npm, gh, chromium | command -v je našel | pouze PATH; spuštění a účty neověřeny |
| hyperframes, hypermotion | command -v nenalezl | nedostupné jako přímé CLI v aktuálním PATH; npx se nespouštělo |

## D13 – motion video u Noxe

HyperFrames ani Motion režie nejsou v aktivním skill katalogu této relace. Na disku existuje distribuční kopie HyperFrames pluginu pod .codex/.tmp/plugins/plugins/hyperframes: manifest uvádí 0.1.2, HeyGen, Apache-2.0 a zdroj https://github.com/heygen-com/hyperframes. To není důkaz aktivace ani runtime verze; neplést s Adiným pluginem/CLI 0.9.9. Její runtime nesmí Nox automaticky přebírat. Hypermotion nebyl nalezen v aktivním katalogu ani PATH; globální absence se netvrdí.

Motion režie: archiv s SHA-256 e575fc2f8acdca1f249c199d9aa59a4fa0717863be93345a81586a9fc87b0843, bez instalace. README dovoluje používat a upravovat; nejde o SPDX licenci ani licenci všech budoucích médií. SKILL.md §HyperFrames mechanics se výslovně odvolává na 0.9.52; Adin lokální runtime je 0.9.9, proto kompatibilitu příkazů teprve ověřit.

Stav schopnosti motion video v tomto běhu: unavailable jako aktivní hotový workflow; nalezené podklady jsou available-unverified. Render není verified ani pro Noxe, ani tímto nezávislým během pro Adu. OS tohoto běhu Linux/WSL; Windows nativní běh, výkon, spotřeba, kvóty a náklady renderu neověřeny.

Bezpečné podmínky případného pilotu: nejprve Michalův souhlas a ověřený Node >=22/runtime bez instalace naslepo; předem prověřené lokální dependency a licence včetně GSAP, fontů a referenčních médií. HyperFrames lokální zdroj dokládá produkční telemetrii default-on a opt-out HYPERFRAMES_NO_TELEMETRY=1 / DO_NOT_TRACK=1. Vypnutí telemetrie samo nezablokuje CDN, API, aktualizace nebo jiné síťové požadavky. Pro offline pilot tedy izolovat síť, použít jen lokální média/fonty/skripty a nevolat TTS ani cizí účty.

Návrh po souhlasu: 10 s / 16:9, syntetické podklady, izolovaná složka; zkontrolovat příkazy pro přesnou verzi, lint/check, snapshot přibližně po 1/3 s, vybrané snímky vizuálně prohlédnout, nejvýš 3 opravy, MP4 a evidence vad. Zaznamenat čas, reálný provoz sítě a náklady; bez běhu žádná čísla nevymýšlet. Fallback nyní: storyboard/HTML návrh jako text, případně předání práce Adě po schválení. Nic nekopírovat z jejího profilu.

## Nejmenší návaznost na D8

D8 již má harness_findings v src/agent_lease_mcp/insight.py: čte vybrané globální konfigurace Claude/Codex, kontroluje existenci programů a frontmatter uživatelských Claude skills. Není to hotový úplný inventář všech projektových profilů, pluginů a skutečné funkčnosti. Znovu použít jeho čtecí mechanismy; až po schválení doplnit zdroj, agent/profil, název, verzi, účel, omezení, installed/enabled a konkrétní ověřenou operaci s časem. Neukládat příkazy, tajemství ani soukromé cesty. Doporučení nástrojů řešit až následným schváleným pilotem z TODO.

Rozhodnutí: UŽ MÁME čtení kódu, review, statickou kontrolu, katalog hosta a D8 základ. PŘIDAT zatím jen přesnější metadata a důkazy jako návrh. NEPŘIDÁVAT automatické instalace, sdílení přihlašovacích profilů ani duplikovaný katalog.
