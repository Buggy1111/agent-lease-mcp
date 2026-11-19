"""Fáze D11: zdraví kódu (Python, `ast`, bez nové závislosti) — jen nálezy, žádné změny.

Heuristiky, ne důkazy: „mrtvý kód“ jsou KANDIDÁTI (dynamické volání přes getattr, entrypointy
z jiných balíčků, pluginy nebo reflexe je nemusí ukázat). Každý nález ověří člověk nebo test.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
import tomllib
from pathlib import Path

MIN_STATEMENTS = 5
_SNAKE = re.compile(r"^_{0,2}[a-z][a-z0-9_]*_{0,2}$")
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox"}


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py")
                  if not any(part in _SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts[:-1]))


class _Strip(ast.NodeTransformer):
    """Normalizace pro porovnání těl: bez názvů proměnných a docstringů."""

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        return ast.copy_location(ast.arg(arg="_"), node)


def _body_key(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if sum(1 for _ in ast.walk(ast.Module(body=body, type_ignores=[])) if isinstance(_, ast.stmt)) < MIN_STATEMENTS:
        return None
    dump = ast.dump(_Strip().visit(ast.Module(body=copy.deepcopy(body), type_ignores=[])))
    return hashlib.sha256(dump.encode()).hexdigest()


def _entrypoints(root: Path) -> set[str]:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    scripts = data.get("project", {}).get("scripts", {}) or {}
    return {str(v).rsplit(":", 1)[-1] for v in scripts.values()}


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(tree: ast.Module, module: str, is_pkg: bool) -> set[str]:
    out = set()
    base = module.split(".") if is_pkg else module.split(".")[:-1]
    for node in tree.body:  # jen importy při načtení modulu; líný import uvnitř funkce cyklus záměrně láme
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(anchor + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            out.add(target)
            out |= {f"{target}.{a.name}" for a in node.names}
    return out


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Silně souvislé komponenty (Tarjan) s více než jedním modulem."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    for v in sorted(graph):
        if v not in index:
            visit(v)
    return sorted(out)


def analyze(root: Path) -> dict:
    root = root.resolve()
    files = _files(root)
    trees: dict[Path, ast.Module] = {}
    errors = []
    for f in files:
        try:
            trees[f] = ast.parse(f.read_text(), filename=str(f))
        except (SyntaxError, UnicodeError, OSError) as exc:
            errors.append({"file": str(f.relative_to(root)), "error": type(exc).__name__})

    # identifikátory a řetězce: co je „použité“ (i dynamicky přes getattr("jméno"))
    used: dict[str, set[Path]] = {}
    for f, tree in trees.items():
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.alias):
                names = [node.asname or node.name.rsplit(".", 1)[-1], node.name.rsplit(".", 1)[-1]]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value)
            for n in names:
                used.setdefault(n, set()).add(f)

    entry = _entrypoints(root)
    dupes: dict[str, list[str]] = {}
    dead, naming = [], []
    for f, tree in trees.items():
        rel = str(f.relative_to(root))
        is_test = "tests" in f.relative_to(root).parts or f.name.startswith("test_")
        exported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets) \
                    and isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
        methods = {id(m) for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for m in c.body}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if (key := _body_key(node)):
                    dupes.setdefault(key, []).append(f"{rel}:{node.lineno} {node.name}")
                # metody se nehodnotí: názvy jako do_GET nebo setUp diktuje rodičovská třída
                if id(node) not in methods and not _SNAKE.match(node.name) and not is_test:
                    naming.append(f"{rel}:{node.lineno} {node.name}")
        if is_test:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name.startswith("__") or name in exported or name in entry:
                continue
            # použití jinde než ve vlastní definici (rekurze se nepočítá jako použití)
            if not (used.get(name, set()) - {f}) and not _used_in_file_outside(tree, node):
                dead.append(f"{rel}:{node.lineno} {name}")

    modules = {_module_name(root, f): f for f in trees}
    graph = {}
    for mod, f in modules.items():
        deps = _imports(trees[f], mod, f.name == "__init__.py")
        graph[mod] = {d for d in deps if d in modules and d != mod}
    return {
        "kind": "health", "root": str(root), "files": len(files), "parse_errors": errors,
        "duplicates": sorted(sorted(v) for v in dupes.values() if len(v) > 1),
        "dead_candidates": sorted(dead), "import_cycles": _cycles(graph), "naming": sorted(naming),
    }


def _used_in_file_outside(tree: ast.Module, definition: ast.AST) -> bool:
    inside = {id(n) for n in ast.walk(definition)}
    name = definition.name  # type: ignore[attr-defined]
    for node in ast.walk(tree):
        if id(node) in inside:
            continue
        if (isinstance(node, ast.Name) and node.id == name) or (isinstance(node, ast.Attribute) and node.attr == name):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(rf"\b{re.escape(name)}\b", node.value):
            return True
    return False


def markdown(report: dict) -> str:
    lines = [f"# Zdraví kódu (D11): {Path(report['root']).name}", "",
             (f"{report['files']} souborů. Heuristiky, ne důkazy: mrtvý kód jsou **kandidáti** "
              "(dynamické volání, pluginy a reflexe nemusí být vidět). Nic se nemaže bez testů, review a souhlasu."), ""]
    sections = (("Duplicitní implementace", [" = ".join(g) for g in report["duplicates"]]),
                ("Kandidáti na mrtvý kód", report["dead_candidates"]),
                ("Cykly importů", [" ↔ ".join(c) for c in report["import_cycles"]]),
                ("Názvy mimo snake_case", report["naming"]),
                ("Soubory, které nejde načíst", [f"{e['file']} ({e['error']})" for e in report["parse_errors"]]))
    for title, items in sections:
        lines += [f"## {title} ({len(items)})", ""] + ([f"- `{i}`" for i in items] or ["- žádné"]) + [""]
    return "\n".join(lines)
