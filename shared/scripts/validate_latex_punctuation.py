"""Preflight checker for shared LaTeX punctuation and line-break rules."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.scripts.latex_overfull_repair import inspect_latex_layout, repair_latex_layout_source


def validate(root: str | Path, *, fix: bool = False) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    root_path = Path(root)
    for path in sorted(root_path.rglob("*.tex")):
        if any(part in {"preamble", "notation", "generated_diagrams", "textbook_figures"} for part in path.relative_to(root_path).parts):
            continue
        source = path.read_text(encoding="utf-8")
        file_findings = inspect_latex_layout(source)
        if fix:
            repaired, _ = repair_latex_layout_source(source)
            if repaired != source:
                path.write_text(repaired, encoding="utf-8")
        for finding in file_findings:
            findings.append({"file": str(path), "line": finding.line, "kind": finding.kind, "message": finding.message})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--fix", action="store_true", help="repair only safe explicit source-line punctuation breaks")
    args = parser.parse_args()
    findings = validate(args.root, fix=args.fix)
    print(json.dumps({"findings": findings, "count": len(findings)}, ensure_ascii=False, indent=2))
    return 1 if findings and not args.fix else 0


if __name__ == "__main__":
    raise SystemExit(main())
