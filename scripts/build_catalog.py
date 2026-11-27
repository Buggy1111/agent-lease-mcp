"""Katalog schopností pro web z ověřených inventur fáze D (docs/research/*-d-capabilities-*.md).

Spuštění: python scripts/build_catalog.py  → src/agent_lease_mcp/assets/catalog.json
Bere jen markdownové tabulky; text buněk zůstává beze změny (web ho escapuje).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {"ada": "docs/research/2026-11-19-d-capabilities-ada.md", "nox": "docs/research/2026-11-19-d-capabilities-nox.md"}
SKIP = {"Stavy", "Význam stavů a důkazy"}


def cells(line: str) -> list[str]:
    return [c.strip().replace("**", "").replace("`", "") for c in line.strip().strip("|").split("|")]


def parse(text: str) -> list[dict]:
    sections, heading, table = [], "", None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = re.sub(r"^\d+\.\s*", "", line[3:].strip())
            table = None
        elif line.startswith("|") and heading not in SKIP:
            row = cells(line)
            if set("".join(row)) <= {"-", " ", ":"}:
                continue
            if table is None:
                table = {"section": heading, "columns": row, "rows": []}
                sections.append(table)
            else:
                table["rows"].append(row)
        elif not line.strip().startswith("|"):
            table = None if line.strip() else table
    return [s for s in sections if s["rows"]]


def main() -> None:
    catalog = {"generated_from": SOURCES, "as_of": "2026-11-19",
               "agents": {agent: parse((ROOT / path).read_text()) for agent, path in SOURCES.items()}}
    out = ROOT / "src/agent_lease_mcp/assets/catalog.json"
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n")
    print(out, {a: sum(len(s["rows"]) for s in v) for a, v in catalog["agents"].items()})


if __name__ == "__main__":
    main()
