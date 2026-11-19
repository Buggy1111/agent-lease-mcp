# Audit UI před redesignem — 6. 11. 2026

Výchozí commit: `d01bf3ed0a74f4060bb4330d7416f574cdf9edee`.
Větev: `nox/ui-redesign-20261106`, izolovaný worktree `~/dev/work/agent-lease-ui-redesign`.
Reference: Michalův screenshot `Snímek obrazovky 2026-11-06 153612.png`.

## Současná implementace a funkce k zachování

Frontend je jediná konstanta `PAGE_HTML` v `src/agent_lease_mcp/webui.py`: vložené CSS a čistý JavaScript bez bundleru a externích knihoven. Python pod a nad konstantou poskytuje existující HTTP API. Redesign mění jen obsah konstanty, testy a dokumentaci.

- Autentizace: jednorázová výměna tokenu z fragmentu za HttpOnly cookie přes `/session`; fragment se okamžitě odstraní. Zachovat CSP a veškeré serverové ověřování.
- Úvodní `GET /api/snapshot?since=0`: `me`, posledních nejvýše 200 `messages`, `peers`, `claims`, `board`, `paused`, nejvýše 30 otevřených `reviews`.
- SSE `GET /api/events?since=<id>`: jednotlivé `message` a pravidelné `room`. Server používá Last-Event-ID pro reconnect; `room` znovu nese posledních 200 zpráv a živý stav. Zachovat jediný stream a deduplikaci podle ID.
- Agenti: aktuální přítomnost, status, provider limity/reset, fronta, aktivní úloha a blokace cest. Offline účastníci v rozbalovacím seznamu; neznámá jména a stav limitů se nesmějí změnit v domnělou dostupnost.
- Zamčené cesty: vlastník, účel a zbývající TTL.
- Zprávy/úkoly/výsledky/broadcast/control: autor, adresát, ID, relativní čas, stav doručení, skutečné seen/accepted a accept_note; delší text lze rozbalit. Systémové příjemky jsou standardně skryté.
- Composer: přepnutí zpráva/úkol, výběr adresáta, broadcast pouze pro zprávy, Enter odesílá a Shift+Enter nový řádek. `/api/send` zachová tělo `{text, kind, to}`.
- Akce úkolů: cancel s potvrzením, retry, schválení karantény s důvodem, schválení checkpoint resume s důvodem. Endpointy i podmínky dostupnosti zůstávají.
- Review: stav, téma, autor/recenzent, přesný commit, verdikt a počet opravných kol. U eskalace lidské grant/close s důvodem; stejná API.
- Nouzové zastavení/obnovení: viditelný horní pruh; důvod se posílá do `/api/pause` či `/api/resume`. Po odpovědi se čte skutečný stav. `paused === undefined` nesmí přepsat poslední známý stav.

## Data a limity návrhu

`messages` obsahují ID, autor/recipient, druh, text, reply_to, delivery state, attempts, last_error, seconds_ago, seen, accepted, accept_note. Neobsahují strukturované kroky, checkpoint obsah, absolutní čas přechodů ani samostatné testové výsledky. Detail proto nabídne skutečná pole a přímo navázané zprávy (`reply_to`), nikoli umělou časovou osu plánovaných fází nebo procenta dokončení. Výsledky testů lze číst v původním textu výsledku; nebudou automaticky vyhodnocovány jako strukturovaný PASS.

Čítače úkolů budou výslovně označené jako počty v načtené historii posledních nejvýše 200 zpráv, ne celodenní/celkové statistiky. `board.queued` a `board.needs_review` zahrnují i jiné druhy zpráv, proto je nelze vydávat za celkové počty úkolů. Agenti a bridge procesy budou rozlišeni; operátor se nezapočte jako agent. Stav systému označuje pouze backendem hlášenou pauzu a stav spojení, nikoli neexistující health check.

Samostatný seznam projektů API neposkytuje. Lze zobrazit explicitní `repo` z existujících review, označené jako repozitáře v review; z `cwd`, textu zpráv ani adresářů se projektové stavy neodvozují. Graf výkonu, úspěšnost, analytics, nastavení a upload příloh z vizualizace nemají existující datový podklad/funkci, proto nevzniknou nefunkční ovládací prvky.

## Konflikty s fází A

Při zahájení nejsou v agent-lease žádné cizí claims. Poslední work state uvádí dokončený 8h chaos, nasazenou fázi A/B3/B4 a A11 čekající na Michalův restart WSL; připravený záznam A11 upozorňuje na transient web službu. Redesign neřeší službu ani restart, nepoužívá provozní databázi nebo tokeny. Běží v samostatném worktree z d01bf3e, bez přepnutí masteru. Žádný blokující konflikt z dostupných údajů nevyplývá. Adě oznámeno zprávou #701. Pokud se objeví konflikt s fází A, implementace se zastaví u návrhu.

## Vizuální směr a ověření

Tmavý dashboard s omezeným fialovým/modrým/zeleným podsvícením, šest skutečných přehledových panelů, levý panel účastníků/cest/repozitářů, střed místnosti s filtry a composerem a pravý detail úkolu/review. Bez externích fontů, CDN, síťových závislostí nebo trvalých animací; respektovat reduced-motion a ovládání klávesnicí.

Ověření: existující Python sada a Ruff, browser testy s izolovanými odpověďmi stávajících API (včetně XSS textů, reconnectu, pauzy, akcí a mobilních šířek). Screenshoty před/po použijí stejný skutečný snímek dostupný přes agent-lease, nikoli fiktivní provozní statistiky. Případná testovací data budou od snímků oddělena a označena.
