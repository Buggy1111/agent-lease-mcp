"""
Text, který se agentovi vstřikuje do kontextu. Čisté funkce — testovatelné bez
hooků i bez databáze.

⚠️ Proč to existuje: `room` a `inbox` jsou nástroje, a nástroj se zavolá jen
když si o to někdo řekne. Agent zabraný do práce si o to neřekne — přesně tak
selhal `docs/AGENT-HANDOFF.md`. Když má koordinace fungovat bez uživatele
u klávesnice, musí přijít SÁMA, přes hook, a to znamená vygenerovat text.

Druhé pravidlo: **mlčet, když není co říct.** Vstřikovat stav při každém promptu
by byl šum, agent by to začal přeskakovat a jsme zpátky u nástěnky, do které se
nikdo nedívá.
"""

from __future__ import annotations

from .models import Claim


def session_briefing(me: str, peers: list[dict], claims: list[Claim], messages: list[dict]) -> str:
    """Uvítání na začátku session: kdo je tu, co je zamčené, co ti někdo vzkázal."""
    others = [p for p in peers if p["agent"] != me and p["active"]]
    foreign = [c for c in claims if c.agent != me]

    if not others and not foreign and not messages:
        return ""  # sám v místnosti a nic nového → neplýtvat kontextem

    lines = [f"[agent-lease] Jsi v místnosti jako `{me}`."]

    if others:
        for p in others:
            status = f" — {p['status']}" if p["status"] else ""
            lines.append(f"  Vedle tebe pracuje **{p['agent']}**{status}"
                         f" (naposled před {p['seen_seconds_ago']} s).")
    else:
        lines.append("  Nikdo další tu právě není.")

    if foreign:
        lines.append("  Rozpracované cizí soubory (NESAHAT, editace se ti odmítne):")
        lines.extend(
            f"    - {c.path} — drží {c.agent}"
            f"{', ' + c.purpose if c.purpose else ''} (zbývá {c.expires_in} s)"
            for c in foreign
        )

    lines.extend(_message_lines(messages))
    lines.append("  Než sáhneš na sdílený adresář nebo pustíš skript, co zapisuje do víc "
                 "souborů, vezmi si `claim`. Po dokončení `release`.")
    return "\n".join(lines)


def prompt_update(me: str, claims: list[Claim], messages: list[dict]) -> str:
    """
    Průběžná injekce: JEN když je co říct. Prázdný řetězec = nic se nevstřikuje.

    Nová cizí zámek hlásit netřeba — na ten agent narazí přes hook s hláškou,
    která rovnou říká, kdo ho drží. Vzkaz je jiná věc: ten by jinak nikdo nikdy
    nepřečetl.
    """
    if not messages:
        return ""
    return "\n".join([f"[agent-lease] Vzkaz do místnosti (`{me}`):", *_message_lines(messages)])


def _message_lines(messages: list[dict]) -> list[str]:
    return [f"    💬 {m['agent']} (před {m['seconds_ago']} s): {m['text']}" for m in messages]
