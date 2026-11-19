# B7 — REA jako mapa pro navázání na existující dashboard

Michal upřesnil účel: **urychlit vývoj Agent Lease podle B7.0 fast-track auditu**, nejen kontrolovat bezpečnost cizích binárek. Proto je k [binární provenienční kontrole](2026-11-07-b7-rea-scanner-provenance.md) doplněna cílená statická rekonstrukce vlastního frontendového rozhraní. Nezačíná se fáze D.

## Vstup a důkaz

Ze `src/agent_lease_mcp/webui.py:PAGE_HTML` přesného nasazeného commitu `9134485371fdd6d1c92c16562aa04d188c3f2125` byl přes Python AST vyzvednut řetězcový literál a z něj jediný script. Python modul se neimportoval, JavaScript ani prohlížeč se nespouštěly. Vstup obsahuje **46 887 B JavaScriptu**; není to snímek uživatelské session, cookies, tokenu nebo živé místnosti.

REA `analyze_javascript_application` přečetla právě `dashboard.js`: 1 JS soubor, 8 081 navštívených AST uzlů, 0 parse failures. Následný omezený pohled vrátil **11 statických endpointových pozorování**. [Identita zdroje](b7-rea/dashboard-source-identity.json), [summary Evidence](b7-rea/dashboard-summary.json), [endpoint Evidence](b7-rea/dashboard-endpoints.json). Souřadnice v Evidence patří extrahovanému JS, nikoli přímo Python souboru.

Následující rozdělení je výsledkem REA endpointové mapy **doplněné cíleným čtením existujících funkcí**, nikoli tvrzením o úplném automatickém call graphu:

| Existující součást | Co znovu použít pro B7 | Co nevytvářet podruhé |
| --- | --- | --- |
| `/api/snapshot?since=0` a dynamický snapshot | Autorizovaný první snímek, commitově vázaný QG payload v budoucím adaptéru | Druhý HTTP server nebo nový databázový klient ve frontendové stránce |
| `EventSource('/api/events?since=' + since)` a `renderRoom(r)` | Stejný SSE transport a řízené překreslení po změně dat | Paralelní polling/scheduler pro dashboard |
| `QG_STATUS`, `qgBadge`, `qgTable` | Existující sadu stavů a tabulkový/detailový render | Druhou vizuální implementaci Quality Gate |
| `renderQualityGate`, `renderQualityProject` | Nahradit pouze zdroj aktuálně prázdného seznamu skutečnými výsledky z budoucího adaptéru | Importovat inspection plán jako provedený výsledek nebo směšovat demo se skutečností |
| `/api/review-grant`, `/api/review-close`, `/api/approve*` | Zachovat existující lidské oprávnění a oddělené B6 review | Druhé schvalování v projektovém configu nebo odvozování approval z QG barvy |
| B7.1 `Inspection.public()` | Kompatibilní názvy stavů/checks, přesný commit, reviewer pending, žádné logy neprovedených kontrol | Přejmenovávat stejné stavy mezi CLI a budoucím UI adaptérem |

Kód potvrzuje, že dnes `renderQualityGate()` bere `qgDemo ? QG_DEMO : []` a `renderRoom` QG zdroj neplní. To je konkrétní místo budoucího napojení. Inspection má `kind=inspection`, `executed=false`, `execution_allowed=false`; takový plán **nesmí** být zaměněn za `quality_run`, ani když se vykreslí podobně. Produkční stránka se tímto průzkumem nemění.

## Jak to urychluje další krok

Rozsah B7.2 lze zúžit na runner + autoritativní výsledek a pozdější adaptér do existujícího snímku/SSE. UI komponenty, transport, escapování a lidské review už existují. B7.1 proto vytváří jen chybějící commitový model/detekci a navazuje na stávající Git hardening; nepřidává další úložiště ani server.

To je ověřený seznam věcí, které se nemají stavět znovu, **ne naměřená úspora hodin nebo modelových tokenů**. U krátkého čitelného modulu zůstává výhodnější přímé čtení zdroje; REA má pomáhat u distribuovaných artefaktů, větších bundle a nejasných vazeb, ne přidávat povinný krok ke každé editaci.

## Omezení

REA výslovně hlásí partial application coverage, 1 913 sémantických unknowns a 2 application unknowns; dynamické `url` a `'/api/' + action` nejsou plně rozřešené endpointy. V kandidátech se objevují také chybně odvozené module references z běžných DOM volání; nejsou přijaty jako skutečné importy. Dva pomocné inventarizované soubory nebyly analyzovány, source selection je přesná.

Nejde o runtime test, úplný call graph ani ověření autorizace serveru. Pokusy o dotaz funkce podle jména nesplnily přísný vnitřní kontrakt traceru (vyžaduje konkrétní fingerprint); takový trace nebyl vydáván za hotový důkaz. Použitelné závěry stojí jen na úspěšné statické analýze, endpointovém pohledu a čtení vlastního zdroje. Autor Nox, nezávislé review pending, žádné zprávy Adě.
