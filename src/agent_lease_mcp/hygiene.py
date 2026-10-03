"""
Vstupní hygiena: meze velikosti, redakce tajemství, označení cizího obsahu.

Čisté funkce bez IO. Zprávy v místnosti putují do kontextu jiného modelu, takže
jsou to data od někoho cizího — ne instrukce (APP-004) — a je rozumné, aby se do
sdílené databáze nedostal omylem vložený klíč (APP-012).
"""

from __future__ import annotations

import re

from .config import MAX_NAME_CHARS, MAX_PATH_CHARS, MAX_SHORT_CHARS, MAX_TEXT_CHARS

# Běžné tvary tajemství. Schválně úzké: falešná redakce by mazala užitečný text.
_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{20,}=*"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def check_len(label: str, value: str, limit: int) -> str:
    if len(value) > limit:
        raise ValueError(f"{label} je delší než {limit} znaků")
    return value


def check_text(text: str) -> str:
    return check_len("text", _CONTROL.sub("", text), MAX_TEXT_CHARS)


def check_name(label: str, value: str) -> str:
    return check_len(label, value, MAX_NAME_CHARS)


def check_short(label: str, value: str) -> str:
    return check_len(label, value, MAX_SHORT_CHARS)


def check_path(value: str) -> str:
    return check_len("cesta", value, MAX_PATH_CHARS)


def untrusted(text: str, limit: int = 600) -> str:
    """
    Obsah od jiného agenta pro vložení do kontextu modelu.

    Nejde o sandbox — model může text stejně poslechnout. Jde o to, aby bylo
    z promptu zřejmé, že je to citace, ne pokyn od uživatele, a aby se z ní
    nedalo vyskočit falešným řádkem „[agent-lease] …".
    """
    flat = " ".join(_CONTROL.sub("", text).split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat.replace("[agent-lease]", "[agent-lease\u200b]")  # zero-width: nelze zfalšovat hlavičku


# Deterministická první linie proti vložení pokynů do cizí zprávy. Nezávislá na
# jakémkoli modelu — Jev i LLM umí nechat pokyny uvnitř textu posunout odpověď,
# regex ne. Schválně jen vzory „přebij pokyny" a „vynes tajemství"; obyčejné
# `rm -rf build` v zadání od kolegy agenta nesmí vyvolat falešný poplach.
_INJECTION = (
    re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|your)\b.{0,30}"
               r"\b(instruction|prompt|rule|guideline)s?"),
    re.compile(r"(?i)\b(ignoruj|zapome[ňn]|p[řr]ehlédni)\b.{0,40}\b(pokyn|instrukc|pravidl|zadání)"),
    re.compile(r"(?i)\b(you are now|from now on you|jsi nyní|od teď jsi)\b"),
    re.compile(r"(?i)\b(print|show|send|cat|vypiš|pošli|ukaž)\b.{0,40}(\.ssh|id_rsa|\.env\b|"
               r"api[ _-]?key|token|password|heslo|credentials)"),
    re.compile(r"(?i)(curl|wget)[^\n|]{0,120}\|\s*(ba|z)?sh\b"),
    re.compile(r"(?i)\bsystem prompt\b|\bpřepiš (svá|svoje) pravidla"),
)


def heuristic_risk(text: str) -> float:
    """0.0 nic nenalezeno; 0.85 při shodě s některým vzorem."""
    return 0.85 if any(p.search(text) for p in _INJECTION) else 0.0
