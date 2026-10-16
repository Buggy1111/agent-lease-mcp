"""
Tenký HTTP klient pro Jev (TypeSafe AI). Žádné rozhodování tady — jen síť,
parsování odpovědi a jeden úzký typ chyby. Oddělené od `jev_guard.py`
schválně: hook pak jde testovat bez sítě, stačí `classify` nahradit fake
funkcí (viz `tests/test_jev_guard.py`).

Tvar requestu/response podle `~/dev/jev-integrace-dokumentace.md` (Izy,
22.9.2026) a přímo ověřeného chování `noul` typu otázky (TypeSafe blog,
LangChain blog — websearch 22.9.2026). Přímé API, ne OpenRouter: endpoint
`https://api.typesafe.ai/v1/systemone` je zdrojově potvrzený; OpenRouterový
tvar requestu pro Jev (`typesafe/jev-1.13`) zatím ne, takže by tu byl jen
hádaný kód.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MODEL = "jev-latest"
_TIMEOUT_SECONDS = 5.0

_INSTRUCTIONS = (
    "Je tenhle shellovy prikaz destruktivni nebo nevratny "
    "(mate nebo prepisuje data, meni systemove nastaveni mimo aktualni projekt, "
    "posila tajemstvi/klice pryc, force-pushuje, vypina nebo restartuje stroj)? "
    "Bezne cteci, buildovaci a testovaci prikazy NEJSOU destruktivni."
)


class JevError(RuntimeError):
    """
    Cokoli, co zabrání dostat validní odpověď od Jevu — chybějící klíč, síť,
    timeout, neočekávaný tvar odpovědi. `jev_guard.py` na tohle vždy
    fail-open pustí dál, stejně jako lease `guard.py` na rozbitou koordinaci.
    """


@dataclass(frozen=True)
class JevAnswer:
    p_destructive: float


def classify_command(command: str) -> JevAnswer:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise JevError("chybí TYPESAFE_API_KEY")

    body = json.dumps(
        {
            "model": _MODEL,
            "state": command,
            "questions": {
                "is_destructive": {"type": "noul", "instructions": _INSTRUCTIONS},
            },
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        _ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError(f"síť selhala: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevError(f"nevalidní JSON: {exc}") from exc

    try:
        value = payload["answers"]["is_destructive"]["noul"]
    except (KeyError, TypeError) as exc:
        raise JevError(f"neočekávaný tvar odpovědi: {exc}") from exc

    if not isinstance(value, (int, float)):
        raise JevError(f"noul není číslo: {value!r}")

    return JevAnswer(p_destructive=float(value))
