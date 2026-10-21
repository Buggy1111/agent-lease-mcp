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
        raise ValueError(f"{label} is longer than {limit} characters")
    return value


def check_text(text: str) -> str:
    return check_len("text", _CONTROL.sub("", text), MAX_TEXT_CHARS)


def check_name(label: str, value: str) -> str:
    return check_len(label, value, MAX_NAME_CHARS)


def check_short(label: str, value: str) -> str:
    return check_len(label, value, MAX_SHORT_CHARS)


def check_path(value: str) -> str:
    return check_len("path", value, MAX_PATH_CHARS)


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
               r"api[ _-]?key|private key|credentials|soukrom[ýy] kl[íi][čc])"),
    re.compile(r"(?i)(curl|wget)[^\n|]{0,120}\|\s*(ba|z)?sh\b"),
    re.compile(r"(?i)\bsystem prompt\b|\bpřepiš (svá|svoje) pravidla"),
)

# Technické reporty a review často musejí nebezpečný řetězec DOSLOVA pojmenovat.
# Samotné uvozovky nestačí (útočník může pokyn prostě ocitovat): snížení rizika
# platí jen při kombinaci citace / inline kódu a blízkého metapopisu, že jde o
# vzor, příklad, filtr nebo planý poplach.
_MENTION_CONTEXT = re.compile(
    r"(?i)(?:\b(example|literal|quoted|phrase|pattern|regex|filter|detect|catch|"
    r"trigger|documentation|hard rules?|false positive|příklad|doslovn|fráz|vzor|"
    r"filtr|zachyt|spustil|výpis|zmínk|planý poplach|pokusy? o převzetí)\b|e\.g\.|např\.)"
)
_ACTION_AFTER_MENTION = re.compile(
    r"(?i)^.{0,30}\b(now|then|teď|nyní|pak)\b.{0,60}"
    r"\b(run|execute|obey|follow|spusť|proveď|udělej|poslechni)\b"
)
_QUOTES = (("`", "`"), ("“", "”"), ("„", "“"), ('"', '"'))


def _quote_bounds(
    text: str, start: int, end: int, opener: str, closer: str,
) -> tuple[int, int] | None:
    left = text.rfind(opener, max(0, start - 300), start)
    if left < 0:
        return None
    if opener == closer:
        # Inline citace nekříží řádek; lichý počet oddělovačů na aktuálním
        # řádku před shodou znamená „uvnitř". Tím nás nerozhodí starší code fence.
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].count(opener) % 2 == 0:
            return None
    elif text.rfind(closer, left + len(opener), start) >= left:
        return None
    right = text.find(closer, end, min(len(text), end + 300))
    return (left, right) if right >= end else None


def _quoted_meta_mention(text: str, match: re.Match) -> bool:
    quote = next(
        ((pair, bounds) for pair in _QUOTES
         if (bounds := _quote_bounds(text, match.start(), match.end(), *pair)) is not None),
        None,
    )
    if quote is None:
        return False
    (opener, _closer), (left, right) = quote
    quoted = text[left + len(opener):right]
    relative_start = match.start() - left - len(opener)
    relative_end = match.end() - left - len(opener)
    remainder = quoted[:relative_start] + quoted[relative_end:]
    # Nízké riziko patří jen HOLÉ citaci vzoru. Jakýkoli další příkaz uvnitř
    # stejné citace znamená plné riziko, i kdyby okolí tvrdilo „example".
    if not re.fullmatch(r"[\s.,;:!?…()\[\]{}—-]*", remainder):
        return False
    if _ACTION_AFTER_MENTION.search(text[match.end():match.end() + 120]):
        return False
    context = text[max(0, match.start() - 220):min(len(text), match.end() + 220)]
    return bool(_MENTION_CONTEXT.search(context))


def heuristic_risk(text: str) -> float:
    """0.0 bez shody; 0.2 citovaná metazmínka; 0.85 pravděpodobný pokus."""
    matches = [match for pattern in _INJECTION for match in pattern.finditer(text)]
    if not matches:
        return 0.0
    return 0.2 if all(_quoted_meta_mention(text, match) for match in matches) else 0.85
