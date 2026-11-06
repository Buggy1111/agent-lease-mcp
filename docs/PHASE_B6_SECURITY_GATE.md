# Fáze B6: bezpečnostní verdikt a kontrola tajných údajů v review

Stav: implementováno 6. 11. 2026 (Ada), čeká na review Noxe. Nenasazeno.

## Co platí

- Při `review request` i `submit` projde MCP nástroj **celý diff review** (`base..head`) a hledá tajné
  údaje v přidaných řádcích (`secret_scan.py`): GitHub/Slack/AWS/Google klíče, `sk-…` (OpenRouter,
  OpenAI), Bearer tokeny, soukromé klíče a obecné přiřazení `*_KEY/SECRET/TOKEN/PASSWORD = "…"` delšímu
  řetězci s písmeny i číslicemi. Ukládá se jen soubor, řádek a druh, **nikdy hodnota**.
- Review bez zadaného `base` dostane jako base rodiče prvního commitu, takže každé kolo kontroluje
  celý rozsah (tajemství ze staršího commitu série neproklouzne).
- **Schválit (`approved`) jde jen**, když:
  1. secret scan proběhl právě na tomto commitu a nic nenašel (nezkontrolovaný commit, třeba
     review založené před B6, je potřeba předložit znovu),
  2. recenzent výslovně dá `security="pass"` (`verdict(..., security="pass")`). Bez toho je verdikt
     odmítnut.
- `changes_requested` jde vždy (volitelně `security="fail"`). Nález v tajných údajích tak vede přes
  opravu nového commitu, ne přes výjimku.
- `review check` / `review_approved` uznává jen schválení s `security="pass"`.
- Web: karta review ukazuje „tajné údaje: N (soubory)“, „bez kontroly tajemství“, nebo „bezpečnost OK“.

Git pro diff běží se stejným zpřísněním jako `resolve_commit` (žádná globální/systémová konfigurace,
`--no-ext-diff --no-textconv`, žádný lazy fetch). Chyba čtení diffu = odmítnutí (fail closed).

## Testy

`tests/test_secret_scan.py` (16): každý druh tajemství, žádné hodnoty v nálezech, odstraněné řádky
se nepočítají, běžný kód není tajemství, soulad s redakcí v `hygiene`, schválení jen s `security=pass`
a čistým scanem, nezkontrolovaný commit, validace nálezů, migrace starší tabulky. V `test_server.py`
konec-konec: commit s klíčem → zablokováno → oprava → schváleno; tajemství ve starším commitu série
se najde i v dalším kole. Plná sada: 568 passed, 1 xfailed.

## Omezení

- Vzory jsou heuristika; nenajdou neobvyklé formáty a mohou hlásit falešný poplach (pak opravit commit,
  nebo rozhodne člověk zavřením review).
- „Před autonomními zápisy“ znamená bránu pro C1/C2: zápisový bridge bude smět sloučit jen commit,
  pro který `review_approved` vrátí schválení.
