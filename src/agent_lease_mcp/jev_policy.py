"""
Pravidla Bash safety-gate: co blokuje natvrdo, kdy volat Jev, jak jeho
odpověď převést na rozhodnutí. Čisté funkce, žádné IO, žádná síť — stejný
seam jako u `policy.py` pro nájmy, a stejně tak je tohle ta část, co se bude
měnit nejčastěji (přibudou vzory, posune se práh).

Schválně oddělené od `policy.py`: ten řeší „kdo drží soubor" (deterministické,
levná cena chyby). Tohle řeší „je příkaz nebezpečný" (pravděpodobnostní,
vysoká cena falešného bloku i falešného průchodu). Různý problém, různá
struktura rozhodování — viz `docs/adr/0003-jev-samostatny-guard.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class GuardVerdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class GuardDecision:
    verdict: GuardVerdict
    reason: str = ""
    source: str = ""  # "hard-rule" | "jev" | "shadow"

    @property
    def blocks(self) -> bool:
        return self.verdict is GuardVerdict.BLOCK


# Natvrdo blokované vzory. Nepotřebují klasifikátor — škoda je jistá a
# nevratná, takže tu není prostor pro pravděpodobnost. Jev se ptá jen na to,
# co sem nespadne. Schválně úzké (stejná zásada jako `policy.script_target`):
# falešný blok by agenta naučil hook obcházet.
_HARD_BLOCK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Volby v libovolném tvaru (`-rf`, `-r -f`, `--recursive --force`) a cíl
        # kořen, home nebo aktuální adresář — i v uvozovkách nebo s `--`.
        re.compile(
            r"\brm\s+(?:(?:-{1,2}[\w-]+|--)\s+)*?"
            r"(?:-{1,2}[\w-]+\s+)*['\"]?(/|/\*|~/?|\$HOME/?|\.)['\"]?\s*(?:[;&|#]|$)"
        ),
        "rm -rf na kořen, home nebo aktuální adresář",
    ),
    (re.compile(r"--no-preserve-root"), "rm bez ochrany kořene"),
    (re.compile(r"\bgit\s+push\b.*(\s-\w*f\w*\b|--force\b|\s\+[\w./-]+)"), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bchmod\s+-R\s+777\b"), "chmod -R 777"),
    (re.compile(r"\bcurl\b[^|]*\|\s*(sudo\s+)?(bash|sh)\b"), "curl | shell"),
    (re.compile(r"\bwget\b[^|]*\|\s*(sudo\s+)?(bash|sh)\b"), "wget | shell"),
    (re.compile(r"\bdd\s+[^\n]*\bof=/dev/"), "dd přímo na blokové zařízení"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "mkfs"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomba"),
)


def hard_block(command: str) -> str | None:
    """Důvod blokace, nebo None. Deterministické, bez Jevu, bez sítě."""
    for pattern, reason in _HARD_BLOCK_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def decide_from_jev(p_destructive: float, threshold: float, enforce: bool) -> GuardDecision:
    """
    `p_destructive` je Jevova `noul` odpověď — P(příkaz je destruktivní).
    Noul nemá vlastní `confidence` pole (viz jev-integrace-dokumentace.md,
    bod 2) — jedna pravděpodobnost popisuje celé ano/ne rozdělení sama.

    V `shadow` režimu (enforce=False) nikdy neblokuje — jen se ptá, aby šlo
    práh nejdřív ověřit na logu, ne naslepo na produkci (doporučení ze
    zdrojové dokumentace, bod 6.2).
    """
    if not enforce:
        return GuardDecision(GuardVerdict.ALLOW, source="shadow")
    if p_destructive >= threshold:
        return GuardDecision(
            GuardVerdict.BLOCK,
            reason=(
                f"[jev-guard] Jev hodnotí příkaz jako destruktivní "
                f"(p={p_destructive:.2f}, práh={threshold:.2f}). "
                f"Pokud je to omyl, zeptej se Michala nebo sniž JEV_GUARD_THRESHOLD."
            ),
            source="jev",
        )
    return GuardDecision(GuardVerdict.ALLOW, source="jev")


# Tajné hodnoty, které nesmí do logu ani k externímu klasifikátoru. Úzké a
# konzervativní: radši zamaskovat o něco víc, než pustit klíč ven.
_VALUE = r"[A-Za-z0-9_\-.+/=%]+"
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Hodnota smí obsahovat jen znaky, které se v tajných klíčích vyskytují. Nikdy
    # `$ ( ) ; | & < > \`` ani mezeru — jinak by šel „zamaskovat" i kus příkazu
    # a klasifikátor by viděl něco jiného, než se spustí (parser differential).
    re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)" + _VALUE),
    re.compile(r"(?i)\b([\w-]*(?:token|secret|password|passwd|api[_-]?key|key)[\w-]*\s*[=:]\s*['\"]?)" + _VALUE),
    re.compile(r"(?i)(--(?:password|token|api-key|secret)[= ]\s*)" + _VALUE),
    re.compile(r"(://[^/\s:@$();|&<>`]+:)" + _VALUE + r"(@)"),
    re.compile(r"()\b(?:sk-[\w-]{16,}|gh[pousr]_\w{20,}|xox[abp]-[\w-]{10,}|AKIA[0-9A-Z]{16}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]+)"),
)
MAX_LOGGED_COMMAND = 2000


def redact(command: str) -> str:
    """Příkaz se zamaskovanými tajnými hodnotami, oříznutý na rozumnou délku."""
    out = command
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + "***" + (m.group(2) if m.re.groups >= 2 else ""), out)
    if len(out) > MAX_LOGGED_COMMAND:
        out = out[:MAX_LOGGED_COMMAND] + "…"
    return out
