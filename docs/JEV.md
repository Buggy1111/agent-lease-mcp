# Jev (TypeSafe AI) v agent-lease

[Jev](https://dealroom.co/news/157162-what-is-jev-typesafe-ais-decision-model-for-software/)
je „System One" model: negeneruje text, na typované otázky nad stavem vrací
**volbu, skóre nebo pravděpodobnost ano/ne s jistotou** za desítky až stovky ms,
dostupný i přes OpenRouter. Hodí se na rozhodování uvnitř softwaru, ne na práci.

Zapnutí (ve výchozím stavu vypnuto): `AGENT_LEASE_JEV=1` + `OPENROUTER_API_KEY`.
Cena je zanedbatelná (zpráva ~1 000 tokenů ≈ 0,00004 USD).

## Kde to dává smysl (a je implementováno)

| Místo | Otázky (atomické, paralelně) | Co s odpovědí |
|---|---|---|
| **Screening cizí zprávy** před vložením do kontextu agenta | přebití pokynů? vynesení tajemství? vyžaduje akci? | riziko ≥ 70 % → zpráva se v kontextu nahradí upozorněním, člověk dostane zprávu, originál zůstává v místnosti. „Nevyžaduje akci" → chat se nepřipomíná (stačí „viděl"), méně šumu. |
| **Ověření výsledku bridge tasku** | odpovídá zadání? je to odmítnutí/chyba? | shoda < 35 % → `needs_review` místo `succeeded`; nejistota → výsledek se bere jako dosud |
| **Routing** `send --to auto` | který z aktivních agentů? (`choice`) | jen při jisté odpovědi, jinak chyba a člověk zadá `--to` |

Všude platí **fail-open** (výpadek Jev nic nezablokuje), **gating podle
`confidence`** (nejistá odpověď se ignoruje) a **rozhodnutí je signál**, nic se
nemaže ani nepřepisuje.

## Co Jev neumí — a proto kolem něj stojí jiné pojistky

Vlastní dokumentace i testy uvádějí: neumí aritmetiku ani porovnání dat, trpí na
víceskokové otázky a hlavně **vložené pokyny v textu mohou posunout jeho odpověď**
([limity](https://www.layer3labs.io/guides/jev-limits)). Proto:

- screening má **deterministický předfiltr** (`hygiene.heuristic_risk`, regexy na
  „ignoruj pokyny", „vypiš ~/.ssh", `curl | sh`…), který běží vždy a nezávisí na modelu;
- nízké riziko **není záruka** — cizí text je dál označený jako nedůvěryhodná citace;
- Jev se nepoužívá na nájmy, TTL, časy ani počítání (to je deterministický kód).

## Prozkoumáno a záměrně NEzapojeno

| Nápad | Proč ne (zatím) |
|---|---|
| Jev jako blokující guard na Bash příkazy | falešné poplachy naučí agenty hook obcházet (důvod, proč se Bash hlídá jen úzce); možné jako **varování** s `AGENT_LEASE_JEV_GUARD=warn` |
| Sémantická kolize nájmů („dělají na tom samém?") | cesty jsou přesné a deterministické, model by přidal chyby |
| Priorita/pořadí tasků podle naléhavosti (`score`) | zvážit po zkušenostech z provozu; pořadí podle id je předvídatelné |
| Výběr modelu (levný/silný) pro OpenRouter worker podle obtížnosti | dobrý další krok: `score` obtížnosti → `--model` lehký/silný |
| Relevance obsahu pro kompaktování kontextu agenta | jiný problém než koordinace; existuje na to samostatný plugin |

## Upozornění

Tvar požadavku/odpovědi je sestavený z veřejných popisů API (typy `choice`,
`score`, `noul`; `state` + `questions`). Proti živému endpointu jsem ho nemohl
ověřit, mapování je proto jen v `jev.build_request` / `parse_response`. Při prvním
nasazení spusť s `AGENT_LEASE_JEV=1` a zkus `agent-lease send --to auto "…"`.
