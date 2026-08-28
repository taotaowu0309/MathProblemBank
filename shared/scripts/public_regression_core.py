from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import unittest
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PYTHON_SOURCE_ROOTS = (
    ROOT / "shared" / "scripts",
    ROOT / "shared" / "ui",
    ROOT / "tools",
)


def iter_python_files() -> list[pathlib.Path]:
    files: set[pathlib.Path] = set()
    for source_root in PYTHON_SOURCE_ROOTS:
        if not source_root.is_dir():
            continue
        files.update(
            path
            for path in source_root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return sorted(files)


def check_source_compiles() -> None:
    for path in iter_python_files():
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")


def check_unused_imports() -> None:
    failures: list[str] = []
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: list[tuple[str, int, str]] = []
        loaded: set[str] = set()

        class Visitor(ast.NodeVisitor):
            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    imports.append(
                        (
                            alias.asname or alias.name.split(".")[0],
                            node.lineno,
                            f"import {alias.name}",
                        )
                    )
                self.generic_visit(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                module = node.module or ""
                for alias in node.names:
                    imports.append(
                        (
                            alias.asname or alias.name,
                            node.lineno,
                            f"from {module} import {alias.name}",
                        )
                    )
                self.generic_visit(node)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load):
                    loaded.add(node.id)

        Visitor().visit(tree)
        for name, line, description in imports:
            if name != "annotations" and name not in loaded:
                failures.append(
                    f"{path.relative_to(ROOT)}:{line}: unused import {description}"
                )
    if failures:
        raise AssertionError("\n".join(failures))


def check_public_core() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "shared.scripts.test_public_core"
    )
    result = unittest.TestResult()
    suite.run(result)
    if result.errors or result.failures:
        details = [message for _test, message in [*result.errors, *result.failures]]
        raise AssertionError("public core tests failed:\n" + "\n".join(details))


def run_step(name: str, function: Any) -> None:
    print(f"[check] {name} ...", flush=True)
    function()
    print(f"[ok] {name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the self-contained math v0.1 public regression subset."
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="run source compilation and unused-import checks only",
    )
    args = parser.parse_args()
    run_step("source compilation", check_source_compiles)
    run_step("unused imports", check_unused_imports)
    if not args.static_only:
        run_step("public core", check_public_core)
    print("[ok] all public regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
