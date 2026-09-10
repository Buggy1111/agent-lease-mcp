# Co dodělat po restartu obou agentů

Ověřeno zatím jen z jedné strany. Tohle si musíme potvrdit spolu, každý ze svého
klienta — a je to úmyslně seznam pro DVA, ne pro člověka.

## 1. Vidíme se navzájem?
Oba zavolejte `room()`. Každý musí vidět toho druhého jako aktivního peera.
Když se někdo objeví jako `unconfigured-*`, nesedí `AGENT_NAME` mezi serverem
a hookem v jeho klientovi.

## 2. Vstřikuje se kontext při startu?
Ověřeno jen ručním spuštěním `agent-lease-context`. V ŽIVÉM klientovi to
potvrzené není ani u jednoho z nás — na startu session má být v kontextu řádek
`[agent-lease] Jsi v místnosti jako …`. Když tam není, hook se nespustil.

## 3. Doručují se vzkazy?
Jeden pošle `say("test doručení")`, druhý napíše cokoli svému uživateli a musí
ten vzkaz dostat vstříknutý do kontextu — jednou, ne opakovaně.

## 4. Uvolňují se nájmy na konci tahu?
Jeden si vezme `claim`, dokončí tah, druhý pak přes `owner()` ověří, že je cesta
volná. Bez tohohle by nájmy visely až do TTL.

## 5. Ostrá zkouška té původní kolize
Codex pustí `bash deploy/make-work-package.sh` v silikon-manageru, Claude Code
zkusí ten skript během běhu editovat. Editace se MUSÍ odmítnout. To je celý
důvod, proč tenhle projekt existuje — dokud tohle neproběhne naživo, je to jen
slib.

## 6. Rozhodnout
Publikovat na PyPI (Michal už tam má `shelly-mcp` a `anonymize-mcp`), nebo to
nechat lokální? Prior art je slabý a hookové vynucení nikdo jiný nemá.
