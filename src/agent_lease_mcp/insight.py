"""Fáze D8–D10: čtecí zprávy nad místností — self-audit, recall benchmark, efektivita.

Nic se nezapisuje: data z místnosti se čtou ze snímku DB (SQLite backup do dočasného
adresáře), konfigurace jen čtením. Výstup prochází `redact`. Návrhy jsou jen text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import statistics
import tempfile
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path

from .hygiene import redact

SEVERITIES = ("high", "medium", "low")


@contextlib.contextmanager
def snapshot(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Konzistentní kopie DB v soukromém dočasném adresáři; ostrá DB se otevře jen pro čtení."""
    with tempfile.TemporaryDirectory(prefix="agent-lease-insight-") as tmp:
        os.chmod(tmp, 0o700)
        copy = Path(tmp) / "room.db"
        src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            src.close()
        dst.row_factory = sqlite3.Row
        try:
            yield dst
        finally:
            dst.close()


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _finding(area: str, severity: str, problem: str, cause: str, evidence: str, proposal: str) -> dict:
    return {"area": area, "severity": severity, "problem": problem, "cause": cause,
            "evidence": evidence, "proposal": proposal}


# ── D8: self-audit harnessu ─────────────────────────────────────────────────


def _load(path: Path) -> tuple[object | None, str | None]:
    """(data, chyba). Chybějící soubor není chyba (None, None)."""
    if not path.exists():
        return None, None
    try:
        text = path.read_text()
        return (tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)), None
    except (OSError, ValueError, UnicodeError) as exc:
        # jen typ a řádek: text chyby parseru může citovat obsah souboru (tajemství)
        line = getattr(exc, "lineno", None)
        return None, type(exc).__name__ + (f" na řádku {line}" if line else "")


def _command_missing(command: str, home: Path) -> str | None:
    """První slovo příkazu (po přiřazeních VAR=…), když ho nejde najít; jinak None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return "(nečitelný příkaz)"
    words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
    if not words:
        return None
    exe = os.path.expanduser(words[0].replace("$HOME", str(home)))
    if "/" in exe:
        return None if os.access(exe, os.X_OK) else Path(exe).name
    return None if shutil.which(exe) else Path(exe).name


def _hook_commands(data: object) -> list[tuple[str, str]]:
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    out = []
    for event, entries in (hooks.items() if isinstance(hooks, dict) else []):
        for entry in entries if isinstance(entries, list) else []:
            for hook in (entry.get("hooks", []) if isinstance(entry, dict) else []):
                if isinstance(hook, dict) and hook.get("type", "command") == "command" and hook.get("command"):
                    out.append((event, str(hook["command"])))
    return out


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
    return fields


def harness_findings(home: Path) -> list[dict]:
    found: list[dict] = []
    configs = {
        "claude settings": home / ".claude" / "settings.json",
        "claude state": home / ".claude.json",
        "codex hooks": home / ".codex" / "hooks.json",
        "codex config": home / ".codex" / "config.toml",
    }
    loaded = {}
    for label, path in configs.items():
        data, err = _load(path)
        loaded[label] = data
        if err:
            found.append(_finding("config", "high", f"{label} nejde načíst", err, str(path),
                                  "opravit syntaxi souboru (klient ho nejspíš celý ignoruje)"))
    for label in ("claude settings", "codex hooks"):
        for event, command in _hook_commands(loaded[label]):
            if (missing := _command_missing(command, home)):
                found.append(_finding("hooks", "high", f"hook {event} spouští program, který neexistuje",
                                      f"nenalezeno: {missing}", f"{configs[label]} ({event})",
                                      "opravit cestu nebo hook odstranit (selhává při každém volání)"))
    servers: list[tuple[str, str, str]] = []
    state = loaded["claude state"]
    if isinstance(state, dict):
        for name, srv in (state.get("mcpServers") or {}).items():
            if isinstance(srv, dict) and srv.get("command"):
                servers.append(("claude", name, str(srv["command"])))
    codex = loaded["codex config"]
    if isinstance(codex, dict):
        for name, srv in (codex.get("mcp_servers") or {}).items():
            if isinstance(srv, dict) and srv.get("command"):
                servers.append(("codex", name, str(srv["command"])))
    for client, name, command in servers:
        if (missing := _command_missing(command, home)):
            found.append(_finding("mcp", "medium", f"{client}: MCP server {name} nemá program",
                                  f"nenalezeno: {missing}", f"{client} MCP {name}",
                                  "opravit příkaz nebo server odebrat (prodlužuje start a hlásí chybu)"))
    names: dict[str, list[str]] = {}
    skills = home / ".claude" / "skills"
    for skill in sorted(skills.glob("*/SKILL.md")) if skills.is_dir() else []:
        try:
            meta = _frontmatter(skill.read_text())
        except (OSError, UnicodeError):
            meta = {}
        if not meta.get("name") or not meta.get("description"):
            found.append(_finding("skills", "medium", "skill bez name/description ve frontmatteru",
                                  "model ho nenajde podle popisu", str(skill),
                                  "doplnit frontmatter `name` a `description`"))
        if meta.get("name"):
            names.setdefault(meta["name"], []).append(str(skill))
    for name, paths in names.items():
        if len(paths) > 1:
            found.append(_finding("skills", "low", f"skill {name} je víckrát", "nejasné, který se použije",
                                  ", ".join(paths), "ponechat jednu kopii"))
    for tool in ("claude", "codex", "git", "bwrap"):
        if not shutil.which(tool):
            found.append(_finding("tools", "low", f"{tool} není v PATH", "nástroj chybí nebo PATH služby je jiná",
                                  tool, "doinstalovat, nebo ověřit PATH v jednotce služby"))
    return found


# Předpony, které do `last_error` píše sám agent-lease; nic jiného se do zprávy nekopíruje.
ERROR_KINDS = ("research", "bridge", "fix", "budget", "checkpoint", "failover", "timeout")


def error_kind(last_error: str | None) -> str:
    """Kategorie ze seznamu známých hodnot, jinak obecně „jiný“; nikdy surový text."""
    text = (last_error or "").strip().lower()
    if not text:
        return "bez důvodu"
    head = text.split(":", 1)[0] if ":" in text else ""
    return head if head in ERROR_KINDS else "jiný"


def room_findings(con: sqlite3.Connection, since: float) -> list[dict]:
    found: list[dict] = []
    tables = _tables(con)
    if "deliveries" not in tables:
        return found
    for state, severity in (("needs_review", "medium"), ("failed", "medium")):
        kinds: dict[str, list[int]] = {}
        for row in con.execute("SELECT message_id, last_error FROM deliveries WHERE state=? AND updated_at>=?",
                               (state, since)):
            kinds.setdefault(error_kind(row["last_error"]), []).append(row["message_id"])
        for kind, ids in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            found.append(_finding("errors", severity, f"{len(ids)}× {state}", f"kategorie důvodu: {kind}",
                                  "úkoly " + ",".join(map(str, ids[:8])),
                                  "projít příčinu; opakuje-li se, zapsat do D1 (příznak → příčina → řešení)"))
    many = con.execute("SELECT COUNT(*) n, GROUP_CONCAT(message_id) ids FROM deliveries "
                       "WHERE attempts>=3 AND updated_at>=?", (since,)).fetchone()
    if many["n"]:
        found.append(_finding("waste", "medium", f"{many['n']} úkolů mělo ≥ 3 pokusy",
                              "opakované spouštění stojí tokeny a čas",
                              "úkoly " + ",".join((many["ids"] or "").split(",")[:8]),
                              "zjistit, proč pokus selhává, než se pustí další"))
    if "task_usage" in tables:
        unrep = con.execute("SELECT COUNT(*) FROM task_usage WHERE at>=? AND source='unreported'",
                            (since,)).fetchone()[0]
        if unrep:
            found.append(_finding("waste", "medium", f"{unrep} pokusů bez reportu spotřeby",
                                  "klient nevrátil usage; strop C3/D7 je pak slepý", "task_usage.source=unreported",
                                  "ověřit výstupní formát klienta (--output-format json)"))
    if "messages" in tables:
        expired = con.execute(
            "SELECT COUNT(*) FROM deliveries d JOIN messages m ON m.id=d.message_id "
            "WHERE d.state='expired' AND m.sent_at>=?", (since,)).fetchone()[0]
        if expired:
            found.append(_finding("waste", "low", f"{expired} zpráv vypršelo nepřečtených",
                                  "zpráva nikoho nezastihla", "deliveries.state=expired",
                                  "posílat úkoly jako task, chat jen pro informaci"))
    return found


def self_audit(home: Path, db_path: Path | None, days: float = 7) -> dict:
    """Doctor se záměrně nevolá: bez brokeru by otevřel Store nad ostrou DB (zápis)."""
    since = time.time() - days * 86400
    findings = harness_findings(home)
    if db_path and db_path.exists():
        with snapshot(db_path) as con:
            findings += room_findings(con, since)
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return {"kind": "self-audit", "generated_at": time.time(), "days": days, "findings": findings}


def self_audit_markdown(report: dict) -> str:
    lines = [f"# Self-audit harnessu ({time.strftime('%Y-%m-%d %H:%M', time.localtime(report['generated_at']))})",
             "", (f"Období: posledních {report['days']:g} dní. Jen čtení; nic se nezměnilo. "
                  "Návrhy provede agent běžnou cestou (úkol → review → člověk)."), ""]
    if not report["findings"]:
        lines.append("Žádné nálezy.")
    for f in report["findings"]:
        lines += [f"## [{f['severity']}] {f['area']}: {f['problem']}", "",
                  f"- Příčina: {f['cause']}", f"- Důkaz: `{f['evidence']}`", f"- Návrh: {f['proposal']}", ""]
    lines.append("Stav koordinace ověří zvlášť `agent-lease doctor`.")
    return redact("\n".join(lines).rstrip() + "\n")


# ── D9: recall benchmark ────────────────────────────────────────────────────


def load_cases(path: Path) -> list[dict]:
    data = tomllib.loads(path.read_text())
    cases = data.get("case")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases file needs at least one [[case]]")
    out = []
    for i, c in enumerate(cases, 1):
        if not isinstance(c, dict) or not str(c.get("query", "")).strip():
            raise ValueError(f"case {i}: query required")
        expect, forbid = c.get("expect", []), c.get("forbid", [])
        if not all(isinstance(x, int) for x in [*expect, *forbid]):
            raise ValueError(f"case {i}: expect/forbid must be integer ids")
        k = c.get("k", 5)
        if not isinstance(k, int) or not 1 <= k <= 50:
            raise ValueError(f"case {i}: k must be 1..50")
        out.append({"query": str(c["query"]), "project": str(c.get("project", "")),
                    "expect": list(expect), "forbid": list(forbid), "k": k})
    return out


def cases_fingerprint(cases: list[dict]) -> str:
    """Baseline se smí porovnat jen se stejnou sadou případů a parametrů."""
    return hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()[:16]


def _search(con: sqlite3.Connection, query: str, project: str, k: int, *, retired: bool = False) -> list[dict]:
    """Přesně to hledání, které dostane agent v D1 (ověřené první, vyřazené jen na vyžádání)."""
    from ._knowledge import KnowledgeMixin

    class _Snap(KnowledgeMixin):
        def _connect(self):
            return contextlib.nullcontext(con)

    con.row_factory = sqlite3.Row
    return _Snap().knowledge_human_search(query, project, include_unverified=True,
                                          include_retired=retired, limit=k)


def _summary(results: list[dict]) -> dict:
    scored = [r for r in results if not r["outdated_case"]]
    n = len(scored)
    return {
        "cases": len(results), "scored": n, "outdated_cases": len(results) - n,
        "precision": sum(r["precision"] for r in scored) / n if n else 0.0,
        "recall": sum(r["recall"] for r in scored) / n if n else 0.0,
        "mrr": sum(r["rr"] for r in scored) / n if n else 0.0,
        "wrong_recall": sum(bool(r["wrong_recall"]) for r in scored),
        "unverified_returned": sum(len(r["unverified_returned"]) for r in scored),
        "retired_diag": sum(bool(r["retired_diag"]) for r in results),
    }


def comparable(report: dict, baseline: dict) -> tuple[dict, dict, int]:
    """Souhrny obou běhů na PRŮNIKU bodovaných případů (změna sady nesmí zkreslit průměry)."""
    def scored(rep):
        return {c.get("key"): c for c in rep.get("cases", []) if c.get("key") and not c.get("outdated_case")}
    now, old = scored(report), scored(baseline)
    common = sorted(now.keys() & old.keys())
    if not common:
        raise ValueError("baseline shares no scored case with this run; not comparable")
    return _summary([now[k] for k in common]), _summary([old[k] for k in common]), len(common)


def recall_bench(con: sqlite3.Connection, cases: list[dict]) -> dict:
    """
    Relevantní = ID v `expect`; případ je „zastaralý“, když očekávaný záznam už neexistuje,
    je vyřazený (stale/rejected) nebo patří jinému projektu — pak se z metrik vynechá.
    Chybné vybavení = vrácené ID z `forbid` v běžném režimu. Vyřazené záznamy se měří zvlášť
    jen jako diagnostika (režim s include_retired), do metrik se nepočítají.
    """
    results = []
    for c in cases:
        expect = set(c["expect"])
        rows = {r["id"]: r for r in con.execute(
            f"SELECT id, status, project FROM knowledge WHERE id IN ({','.join('?' * len(expect))})",
            sorted(expect))} if expect else {}
        outdated = sorted(i for i in expect if i not in rows or rows[i]["status"] in ("stale", "rejected")
                          or (c["project"] and rows[i]["project"] not in ("", c["project"])))
        hits = _search(con, c["query"], c["project"], c["k"])
        ids = [h["id"] for h in hits]
        statuses = [h["status"] for h in hits]
        relevant = [i for i in ids if i in expect]
        first = next((n for n, i in enumerate(ids, 1) if i in expect), None)
        diag = _search(con, c["query"], c["project"], c["k"], retired=True)
        results.append({
            "key": cases_fingerprint([c]), "query": c["query"], "k": c["k"], "returned": ids, "statuses": statuses,
            "outdated_case": outdated,
            "precision": len(relevant) / len(ids) if ids else (1.0 if not expect else 0.0),
            "recall": len(set(relevant)) / len(expect) if expect else 1.0,
            "rr": 1 / first if first else 0.0,
            "wrong_recall": sorted(set(ids) & set(c["forbid"])),
            "unverified_returned": [h["id"] for h in hits if h["status"] == "unverified"],
            "retired_diag": [h["id"] for h in diag if h["status"] in ("stale", "rejected")],
        })
    summary = _summary(results)
    return {"kind": "recall-bench", "generated_at": time.time(), "fingerprint": cases_fingerprint(cases),
            "summary": summary, "cases": results}


def recall_markdown(report: dict, baseline: dict | None = None) -> str:
    s, base, common = report["summary"], None, 0
    if baseline:
        s, base, common = comparable(report, baseline)
    lines = ["# Recall benchmark paměti (D1)", "",
             ("Režim jako u agenta: ověřené první, vyřazené jen na vyžádání. Neověřené záznamy jsou "
              "tvrzení, ne fakta; benchmark je jen počítá, nepovyšuje."), "",
             f"Sada `{report['fingerprint']}`." + (f" Srovnání s baseline jen na průniku {common} bodovaných "
                                                    "případů (stejný dotaz i parametry)." if baseline else ""), "",
             "| metrika | hodnota |" + (" baseline | rozdíl |" if baseline else ""),
             "| --- | --- |" + (" --- | --- |" if baseline else "")]
    for key in ("cases", "scored", "outdated_cases", "precision", "recall", "mrr", "wrong_recall",
                "unverified_returned", "retired_diag"):
        val = s[key]
        row = f"| {key} | {val:.3f} |" if isinstance(val, float) else f"| {key} | {val} |"
        if base:
            old = base.get(key)
            if isinstance(old, (int, float)):
                row += f" {old:.3f} | {val - old:+.3f} |" if isinstance(val, float) else f" {old} | {val - old:+d} |"
            else:
                row += " – | – |"
        lines.append(row)
    lines += ["", "| dotaz | vráceno (stav) | P | R | RR | chybně | zastaralý případ | vyřazené (diagnostika) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in report["cases"]:
        got = ", ".join(f"{i} ({st})" for i, st in zip(r["returned"], r["statuses"], strict=True)) or "–"
        lines.append(f"| {r['query'][:60]} | {got} | {r['precision']:.2f} | {r['recall']:.2f} | {r['rr']:.2f} | "
                     f"{r['wrong_recall'] or '–'} | {r['outdated_case'] or '–'} | {r['retired_diag'] or '–'} |")
    return redact("\n".join(lines) + "\n")


# ── D10: efektivita ─────────────────────────────────────────────────────────

MIN_SAMPLE = 5


def efficiency(con: sqlite3.Connection, since: float) -> dict:
    """
    Kohorta: úkoly odeslané od `since`. Všechny historické pokusy úkolu se čtou přes
    `message_id` (failover přepíše příjemce doručenky, starší pokusy jiného příjemce se tím
    neztratí). Pokus se počítá skupině příjemce × model (skutečný; u nehlášeného pokusu
    požadovaný) × druh, ve které běžel. „Dokončení“ (stav doručení `succeeded`, NE ověřená
    správnost) dostane jen poslední pokus finálního příjemce. Pokus bez reportu nebo s
    neúplnými tokeny (`_NO_TOKENS`) znamená neznámé tokeny; skupina pak nemá průměr.
    """
    from ._usage import _NO_TOKENS, _TOKENS
    tables = _tables(con)
    if not {"task_usage", "deliveries", "messages"} <= tables:
        return {"kind": "efficiency", "generated_at": time.time(), "groups": []}
    final = {r["message_id"]: r for r in con.execute(
        "SELECT d.message_id, d.recipient, d.state, d.updated_at, m.sent_at, m.kind FROM deliveries d "
        "JOIN messages m ON m.id=d.message_id WHERE m.sent_at>=?", (since,))}
    rows = con.execute(
        f"SELECT u.message_id, u.recipient, u.attempt, u.at, u.source, u.model, u.requested_model, u.cost_usd, "
        f"({_TOKENS}) AS tokens, {_NO_TOKENS} AS no_tokens FROM task_usage u "
        "JOIN messages m ON m.id=u.message_id WHERE m.sent_at>=? ORDER BY u.message_id, u.at, u.attempt",
        (since,)).fetchall()
    last_of_final: dict[int, tuple] = {}
    for r in rows:
        d = final.get(r["message_id"])
        if d is not None and r["recipient"] == d["recipient"]:
            last_of_final[r["message_id"]] = (r["recipient"], r["attempt"], r["at"])
    groups: dict[tuple, dict] = {}
    for r in rows:
        d = final.get(r["message_id"])
        if d is None:
            continue
        unreported = r["source"] == "unreported"
        model = (r["model"] if not unreported else None) or r["requested_model"] or "?"
        if not unreported and r["requested_model"] and r["requested_model"] != r["model"]:
            model = f"{r['model']} (chtěno {r['requested_model']})"
        g = groups.setdefault((r["recipient"], model, d["kind"]), {
            "recipient": r["recipient"], "model": model, "kind": d["kind"], "tasks": set(), "completed": [],
            "attempts": 0, "tokens": 0, "tokens_unknown": 0, "unreported": 0, "cost_usd": 0.0, "cost_unknown": 0})
        g["tasks"].add(r["message_id"])
        g["attempts"] += 1
        g["unreported"] += unreported
        if unreported or r["no_tokens"]:
            g["tokens_unknown"] += 1
        else:
            g["tokens"] += r["tokens"]
        if r["cost_usd"] is None:
            g["cost_unknown"] += 1
        else:
            g["cost_usd"] += r["cost_usd"]
        if d["state"] == "succeeded" and last_of_final.get(r["message_id"]) == (r["recipient"], r["attempt"], r["at"]):
            g["completed"].append(max(0.0, (d["updated_at"] or 0) - (d["sent_at"] or 0)))
    out = []
    for g in groups.values():
        tasks, done = g.pop("tasks"), g.pop("completed")
        g.update({
            "task_count": len(tasks), "completed": len(done),
            "completion_rate": len(done) / len(tasks) if tasks else 0.0,
            "attempts_per_task": g["attempts"] / len(tasks) if tasks else 0.0,
            "tokens_per_completion": g["tokens"] / len(done) if done and not g["tokens_unknown"] else None,
            "cost_per_completion": g["cost_usd"] / len(done) if done and not g["cost_unknown"] else None,
            "median_seconds": statistics.median(done) if done else None,
            "small_sample": len(done) < MIN_SAMPLE,
        })
        out.append(g)
    out.sort(key=lambda g: (g["kind"], g["recipient"], g["model"]))
    return {"kind": "efficiency", "generated_at": time.time(), "groups": out}


def efficiency_markdown(report: dict, days: float) -> str:
    lines = [f"# Efektivita úkolů odeslaných za posledních {days:g} dní (D10)", "",
             ("„Dokončeno“ je stav doručení (`succeeded`), ne ověřená správnost; cena na dokončení je proto "
             "jen popisná. Pokus se počítá příjemci, který ho spustil; dokončení jen poslednímu pokusu finálního příjemce. Žádná univerzální hodnota ani "
             f"automatická volba modelu; skupiny s méně než {MIN_SAMPLE} dokončenými se neporovnávají."), ""]
    if not report["groups"]:
        lines.append("Žádná změřená data.")
    else:
        lines += [("| druh | agent | model | úkoly | dokončeno | pokusy/úkol | tokeny/dokončení | "
                   "USD/dokončení | neznámé tokeny | neznámá cena | medián s | |"),
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

        def fmt(v, spec):
            return format(v, spec) if v is not None else "–"
        for g in report["groups"]:
            lines.append(f"| {g['kind']} | {g['recipient']} | {g['model']} | {g['task_count']} | "
                         f"{g['completion_rate']:.0%} | {g['attempts_per_task']:.2f} | "
                         f"{fmt(g['tokens_per_completion'], ',.0f')} | {fmt(g['cost_per_completion'], '.4f')} | "
                         f"{g['tokens_unknown']} | {g['cost_unknown']} | {fmt(g['median_seconds'], '.0f')} | "
                         f"{'malý vzorek' if g['small_sample'] else ''} |")
    if any(g["unreported"] for g in report["groups"]):
        lines += ["", "Pokusy bez reportu spotřeby jsou mezi neznámými tokeny; jejich skupina nemá průměr."]
    return redact("\n".join(lines) + "\n")
