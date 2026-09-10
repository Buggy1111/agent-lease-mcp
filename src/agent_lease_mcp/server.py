"""
MCP server: společná místnost pro Claude Code a Codex.

Plocha nástrojů je schválně malá. Ověřená sada z AgentRoomu (arXiv 2608.23740)
je claim/release/state/broadcast/read — víc nástrojů znamená víc příležitostí,
aby si agent vybral ten špatný. Navíc je jen `room` (jedno levné volání na
kontrolní bod) a `history` (odpověď na „proč mě to zablokovalo").

Slupka nad `Store`, žádná servisní vrstva mezi tím: šest nástrojů, které jen
překládají argumenty na volání store. Vrstva navíc by tu byla jen na proklikávání.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from .config import Settings
from .store import Store

mcp = FastMCP("agent-lease")
_settings = Settings.from_env()
_store = Store(settings=_settings)


@mcp.tool
def claim(paths: list[str], purpose: str = "", ttl_seconds: int | None = None) -> dict:
    """
    Vezmi si nájem na soubory nebo adresáře, které se chystáš měnit.

    Buď dostaneš všechny, nebo žádnou. Když je něco obsazené, vrátí se, kdo to
    drží a na jak dlouho — počkej, vezmi si jinou práci, nebo se ozvi přes `say`.
    Nájem sám vyprší, takže spadlý agent repo nezablokuje.

    Nájem na adresář pokrývá i soubory pod ním. Editace volného souboru si nájem
    vezme sama (hook), tohle je na rezervaci dopředu — typicky než pustíš skript,
    který sahá na víc souborů najednou.
    """
    result = _store.claim(paths, agent=_settings.agent, purpose=purpose, ttl_seconds=ttl_seconds)
    _store.heartbeat(_settings.agent, status=purpose or None, cwd=os.getcwd())
    if result.ok:
        return {"ok": True, "granted": result.granted}
    return {"ok": False, "granted": [], "conflicts": [c.as_dict() for c in result.conflicts]}


@mcp.tool
def release(paths: list[str] | None = None) -> dict:
    """
    Vrať nájem, jakmile jsi hotový. Bez `paths` vrátí všechno tvoje.

    Držet nájem „pro jistotu" blokuje druhého a nikoho to nechrání.
    """
    released = (
        _store.release_all(_settings.agent)
        if not paths
        else _store.release(paths, _settings.agent)
    )
    return {"released": released}


@mcp.tool
def owner(path: str) -> dict:
    """Kdo drží tuhle cestu (i přes nájem na adresář nad ní)?"""
    holder = _store.holder_of(path)
    return holder.as_dict() if holder else {"path": path, "held_by": None}


@mcp.tool
def say(text: str) -> dict:
    """
    Vzkaz do místnosti — druhému agentovi i Michalovi.

    ⚠️ Není to živý chat. MCP je pull: protistrana si vzkaz přečte, až se sama
    podívá (`room` nebo `inbox`). Nečekej odpověď obratem.
    """
    msg_id = _store.say(_settings.agent, text)
    _store.heartbeat(_settings.agent, cwd=os.getcwd())  # status patří práci, ne volání
    return {"id": msg_id}


@mcp.tool
def inbox(since_id: int = 0, limit: int = 50) -> dict:
    """Vzkazy novější než `since_id`. Kurzor si drž mezi voláními."""
    messages = _store.inbox(since_id=since_id, limit=limit)
    return {"messages": messages, "last_id": messages[-1]["id"] if messages else since_id}


@mcp.tool
def room(status: str = "") -> dict:
    """
    Stav místnosti jedním voláním: kdo je tu, co je zamčené, co je nového.

    Volej na začátku práce a po dokončení kroku. `status` zároveň ohlásí, na čem
    děláš, takže druhý agent nemusí hádat.
    """
    _store.heartbeat(_settings.agent, status=status or None, cwd=os.getcwd())
    return {
        "me": _settings.agent,
        "peers": _store.peers(),
        "claims": [c.as_dict() for c in _store.claims()],
        "recent_messages": _store.inbox(since_id=0, limit=10),
    }


@mcp.tool
def history(path: str | None = None, limit: int = 30) -> dict:
    """
    Kdo na co sáhl a jak to dopadlo (nájem, odmítnutí, zablokovaná editace).

    Na tohle se ptáš po kolizi: „proč mi to zablokovalo editaci a kdo tam sahal".
    """
    return {"events": _store.history(limit=limit, path=path)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
