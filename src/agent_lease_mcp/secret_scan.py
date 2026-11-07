"""
Kontrola tajných údajů v diffu před schválením review (fáze B6).

Hledá jen v PŘIDANÝCH řádcích. Nález nese soubor, řádek a druh, nikdy samotnou
hodnotu (výsledek se ukládá do sdílené DB a zobrazuje ve webu).
Vzory: úzké tvary z `hygiene` + obecné přiřazení klíče/hesla dlouhému řetězci.
"""

from __future__ import annotations

import re

# Stejné tvary jako `hygiene._SECRET_PATTERNS` (test hlídá, že se nerozejdou) + obecné přiřazení.
_PATTERNS = (
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("api-key-sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-token", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("assigned-secret", None),  # lineární parser níže, ne regex
)

# Přiřazení dlouhé hodnoty s písmeny i číslicemi (`api_key = "…"`, `TOKEN=…` v env souboru).
# Regexem to nejde zároveň přesně a bez rizika ReDoS (Nox #9), proto malý lineární parser
# bez umělých limitů na délku jména, mezer ani hodnoty:
#  - jméno = souvislý úsek [\w.-] obsahující klíčové slovo, za ním mezery a `=` nebo `:`;
#  - literál v uvozovkách smí obsahovat cokoli kromě mezer (i závorky) a uzavírací uvozovka
#    nemusí být blízko – dlouhý token se pozná podle prvních _WINDOW znaků; mezera literál
#    končí jako u původního vzoru, jinak by uzavírací uvozovka v shellu
#    (`X=$(grep "^X=" … | cut -d= -f2)`) vypadala jako otevírací a hodnotou by byl zbytek řádku;
#  - hodnota bez uvozovek je celý úsek bez mezer, uvozovek a závorek; když za ním hned
#    následuje `(`, je to volání funkce v kódu (např. secrets.token_urlsafe), ne tajemství.
# Konce úseků se spočítají jedním průchodem za řádek a každé `=`/`:` se posuzuje jednou,
# takže ani řádky typu „token:a1token:a1…(“ nebo „token token … “ nejsou kvadratické.
_KEYWORD = re.compile(r"(?i)api[_-]?key|secret|token|passw(?:or)?d|pwd")
_MIN_LEN = 16
_WINDOW = 512
_QUOTES = "\"'"


class _Tables:
    """
    Konce úseků a nejbližší číslice/písmeno pro každou pozici řádku, jedním průchodem odzadu.
    Díky nim se každé `=`/`:` posoudí v konstantním čase.
    """

    def __init__(self, text: str):
        n = len(text)
        self.name, self.blank, self.bare, self.quoted, self.digit, self.alpha = (
            [n] * (n + 1) for _ in range(6))
        for i in range(n - 1, -1, -1):
            ch = text[i]
            self.name[i] = self.name[i + 1] if ch.isalnum() or ch in "_.-" else i
            self.blank[i] = self.blank[i + 1] if ch in " \t" else i
            self.bare[i] = i if ch in _QUOTES or ch in "()" or ch.isspace() else self.bare[i + 1]
            self.quoted[i] = i if ch in _QUOTES or ch.isspace() else self.quoted[i + 1]
            self.digit[i] = i if "0" <= ch <= "9" else self.digit[i + 1]
            self.alpha[i] = i if "a" <= ch <= "z" or "A" <= ch <= "Z" else self.alpha[i + 1]

    def looks_secret(self, start: int, end: int) -> bool:
        """Hodnota [start, end) je dost dlouhá a v prvních _WINDOW znacích má číslici i písmeno."""
        stop = min(end, start + _WINDOW)
        return end - start >= _MIN_LEN and self.digit[start] < stop and self.alpha[start] < stop


def _has_assigned_secret(text: str) -> bool:
    t: _Tables | None = None
    seen: set[int] = set()
    for match in _KEYWORD.finditer(text):
        if t is None:
            t = _Tables(text)
        sep = t.blank[t.name[match.end()]]
        if sep >= len(text) or text[sep] not in "=:" or sep in seen:
            continue
        seen.add(sep)
        at = t.blank[sep + 1]
        if at >= len(text):
            continue
        if text[at] in _QUOTES:
            if t.looks_secret(at + 1, t.quoted[at + 1]):
                return True
            continue
        end = t.bare[at]
        if end < len(text) and text[end] == "(":
            continue  # volání v kódu
        if t.looks_secret(at, end):
            return True
    return False

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MAX_FINDINGS = 50


def scan_diff(diff: str) -> list[dict]:
    """
    Nálezy v přidaných řádcích unified diffu: [{file, line, kind}], bez hodnot.

    Hlavičky (`diff --git`, `---`, `+++`) se poznají jen MIMO úsek: uvnitř úseku se řídí
    počty z `@@ -a,b +c,d @@`. Jinak by přidaný řádek „++ tajemství“ (v diffu „+++ …“)
    vypadal jako hlavička a prošel bez kontroly.
    """
    findings: list[dict] = []
    path, line_no, old_left, new_left = "", 0, 0, 0
    # Jen podle LF jako git: VT, FF, CR, NEL… uvnitř řádku jsou obsah, ne konec řádku
    # (str.splitlines by z jednoho git řádku udělalo dva a marker přeskočilo, Nox #5).
    for raw in diff.split("\n"):
        if raw.startswith(("Binary files ", "GIT binary patch")):
            findings.append({"file": path or "?", "line": 0, "kind": "unscannable-binary"})
            continue
        if old_left <= 0 and new_left <= 0:
            if raw.startswith("+++ "):
                target = raw[4:].strip()
                path = target.removeprefix("b/")
                continue
            hunk = _HUNK.match(raw)
            if hunk:
                line_no = int(hunk.group(3))
                old_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
                new_left = int(hunk.group(4)) if hunk.group(4) is not None else 1
            continue  # ostatní hlavičky a metadata mezi úseky
        if raw.startswith("\\"):
            continue  # „\ No newline at end of file“ se do počtů nepočítá
        if raw.startswith("+"):
            new_left -= 1
            added = raw[1:]
            for kind, pattern in _PATTERNS:
                if pattern.search(added) if pattern else _has_assigned_secret(added):
                    findings.append({"file": path, "line": line_no, "kind": kind})
                    break
            line_no += 1
            if len(findings) >= MAX_FINDINGS:
                break
        elif raw.startswith("-"):
            old_left -= 1
        else:
            old_left -= 1
            new_left -= 1
            line_no += 1
    return findings
