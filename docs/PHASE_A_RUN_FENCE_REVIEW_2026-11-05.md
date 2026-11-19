# Review run-fence opravy (Nox #605 / úkol #622), 5. 11. 2026

**Reviewer:** Ada (claude-code), nezávisle na autorovi (Nox).
**Předmět:** základ `17ef19b` + patch `candidate.patch`, SHA-256 `a5be59a857c0dfa9989f962e2b6cdbd5f6a8922eef21d3f8aeb37ae4950ca370`
(ověřeno: soubor v `~/dev/work/agent-lease-chaos-20261105-fixed/` i necommitnutý `git diff` pracovní kopie mají stejný hash).

## Verdikt: **PASS**

## Nález, který oprava řeší (potvrzuji)
Moje recovery prázdného markeru v `bd382b4` předpokládala, že marker starší než 30 s nemůže patřit živému `acquire`.
To neplatí při SIGSTOP nebo zaseknutí plánovače mezi `os.open(O_EXCL)` a `flock`:
1. recovery marker smaže,
2. další launcher vytvoří nový inode a spustí příkaz,
3. původní owner po probuzení zamkne starý, už smazaný inode a spustí příkaz souběžně.

Tím porušuje invariant „nikdy 2×“. Byla to moje chyba, Nox ji správně našel a deterministickým testem doložil.

## Bezpečnost
- `acquire` drží blokující `flock` rodičovského adresáře přes celé `O_EXCL create → flock(marker) → zápis`. Adresář je stabilní objekt, smazání ani výměna markeru ho nemění.
- `try_recover` bere tentýž zámek `LOCK_NB`. Pokud ho drží živý creator, vrátí `False` a nic nesmaže. Mezera create-before-flock je tím uzavřená.
- Uvnitř zámku se neotevírá žádný další blokující zámek, takže nehrozí deadlock. Ostatní `flock` v kódu (`launch_gate`: soubor `.launch-lock`; `lifecycle._wake_lock`: soubory v `wake/`) zamykají jiné objekty, kolize s adresářovým zámkem nehrozí.
- `release()` a `record_cgroup()` běží mimo adresářový zámek. To je v pořádku: owner v tu dobu drží `flock` vlastního markeru, takže recovery (`LOCK_NB` na markeru) ho nemůže uklidit.
- Chyba otevření adresáře (např. symlink, `O_NOFOLLOW`) v `acquire` vyhodí `OSError`, ne `FileExistsError`. Bridge pak zůstane blokovaný (fail-closed, `17ef19b`). V `try_recover` vede k `False`.
- 30s grace pro prázdný marker zůstává jako druhá pojistka. Je teď nadbytečná, ale neškodí.

## Liveness
- Adresářový zámek drží `acquire` jen po dobu několika syscallů. Kernel ho uvolní i při pádu procesu (zavření fd), což dokládá test se skutečným podprocesem (`exit 23`).
- `try_recover` drží zámek nejdéle po dobu `recover_cgroup` (kill + čekání, timeout 5 s). Blokující `acquire` tedy čeká nejvýš jednotky sekund.
- Zámek je společný všem agentům v adresáři DB. Souběh vede jen ke krátkému čekání, nebo k odložené recovery v další smyčce bridge, nikdy k trvalému zablokování.

## Testy
- 2 nové regrese:
  - pozastavený živý creator po `O_EXCL` s markerem starým 120 s nesmí být uklizen; před opravou FAIL podle Noxe #604,
  - creator zemřel mezi create a flock, recovery pak povolí další launch.
- Moje kontrola: na zmrazeném snapshotu `fence or cgroup` (test_run_fence + test_bridge) **23 passed** (PYTHONPATH na snapshot, bez zápisu do snapshotu).
- Plnou sadu 499 passed a krátký chaos (276 úkolů, 0 pending) uvádí Nox, já je neopakovala.

## Drobnosti (neblokující)
- Komentář u `EMPTY_MARKER_GRACE` by mohl říct, že grace je po opravě jen druhá pojistka.
- Rozhodující důkaz je 8h chaos tohoto přesného snímku, běží od 16:18:09. Výsledek tohoto review se na jiný kód nepřenáší.

## Mez review
Kód i testy jsem četla a cílené testy spustila; patch, snapshot ani běžící test jsem neměnila. Nic nebylo pushnuto, mergnuto ani nasazeno.
