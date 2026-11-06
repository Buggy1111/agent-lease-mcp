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
    ("assigned-secret", re.compile(
        r"(?i)\b[\w.-]*(api[_-]?key|secret|token|passw(or)?d|pwd)[\w.-]*\s*[:=]\s*"
        r"[\"']?(?=[^\"'\s]*[0-9])(?=[^\"'\s]*[A-Za-z])[^\"'\s]{16,}")),
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MAX_FINDINGS = 50


def scan_diff(diff: str) -> list[dict]:
    """Nálezy v přidaných řádcích unified diffu: [{file, line, kind}], bez hodnot."""
    findings: list[dict] = []
    path, line_no = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = target.removeprefix("b/")
            continue
        if raw.startswith(("--- ", "diff --git")):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if raw.startswith("+"):
            added = raw[1:]
            for kind, pattern in _PATTERNS:
                if pattern.search(added):
                    findings.append({"file": path, "line": line_no, "kind": kind})
                    break
            line_no += 1
            if len(findings) >= MAX_FINDINGS:
                break
        elif not raw.startswith(("-", "\\")):
            line_no += 1
    return findings
