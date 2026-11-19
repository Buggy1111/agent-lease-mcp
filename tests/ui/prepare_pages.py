"""Extract actual PAGE_HTML versions for isolated browser checks (stdlib only)."""

import argparse
import ast
import subprocess
from pathlib import Path


def page_html(source: str) -> str:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PAGE_HTML" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("PAGE_HTML is missing")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="d01bf3e")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp"))
    args = parser.parse_args()
    source = Path("src/agent_lease_mcp/webui.py").read_text()
    original = subprocess.run(
        ["git", "show", f"{args.base}:src/agent_lease_mcp/webui.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    # No HTTP handlers, authentication, services, or backend code may change.
    assert source[:source.index("PAGE_HTML =")] == original[:original.index("PAGE_HTML =")]
    assert source[source.index("\ndef run("):] == original[original.index("\ndef run("):]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for version, code in (("before", original), ("after", source)):
        (args.output_dir / f"agent-lease-ui-{version}.html").write_text(page_html(code))
    script = page_html(source).split("<script>", 1)[1].split("</script>", 1)[0]
    (args.output_dir / "agent-lease-ui-script.js").write_text(script)
    print("Backend byte-identical outside PAGE_HTML; before/after pages extracted.")


if __name__ == "__main__":
    main()
