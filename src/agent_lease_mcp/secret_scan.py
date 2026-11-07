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
    # Přiřazení dlouhé hodnoty s písmeny i číslicemi: buď řetězcový literál (uvnitř smí být
    # cokoli, i závorky), nebo hodnota bez uvozovek (env soubor). Výraz v kódu bez uvozovek
    # (např. volání uuid4().hex přiřazené do proměnné tokenu) tajemství není. Délka omezena a kvantifikátory posesivní,
    # ať dlouhý řádek nevyvolá katastrofické zpětné vyhledávání (ReDoS).
    ("assigned-secret", re.compile(
        r"(?i)\b[\w.-]{0,64}(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)[\w.-]{0,64}\s{0,8}[:=]\s{0,8}"
        r"(?:([\"'])(?=[^\"'\n]{0,512}[0-9])(?=[^\"'\n]{0,512}[A-Za-z])[^\"'\n]{16,512}+\1"
        r"|(?![A-Za-z_][\w.]*\()(?=[^\"'\s()]{0,512}[0-9])(?=[^\"'\s()]{0,512}[A-Za-z])"
        r"[^\"'\s()]{16,512}+(?![(.\w]))")),
)
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
                if pattern.search(added):
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
