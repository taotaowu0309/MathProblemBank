from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
THEOREM_ENVIRONMENTS_SOURCE_PATH = (
    ROOT_DIR / "shared" / "templates" / "general_theorem_environments.tex"
)
THEOREM_ENVIRONMENTS_FILENAME = "theorems.tex"


def shared_theorem_environments_tex() -> str:
    source = THEOREM_ENVIRONMENTS_SOURCE_PATH.read_text(encoding="utf-8")
    theorem_environments = (
        "theorem",
        "lemma",
        "proposition",
        "corollary",
        "definition",
        "example",
        "exercise",
        "remark",
    )
    required_markers = [
        rf"\newtheorem{{{environment}}}" for environment in theorem_environments
    ]
    required_markers.extend(
        rf"\tcolorboxenvironment{{{environment}}}{{"
        for environment in theorem_environments
    )
    required_markers.extend(
        (
            r"\RequirePackage{needspace}",
            r"\RequirePackage{etoolbox}",
            r"\newenvironment{solution}",
            r"\MPBSolutionHeading",
        )
    )
    missing = [marker for marker in required_markers if marker not in source]
    if missing:
        raise RuntimeError(
            "The shared theorem environments are incomplete: " + ", ".join(missing)
        )
    return source.rstrip() + "\n"


def sync_theorem_environments(preamble_dir: Path) -> Path:
    preamble_dir.mkdir(parents=True, exist_ok=True)
    target = preamble_dir / THEOREM_ENVIRONMENTS_FILENAME
    source = shared_theorem_environments_tex()
    if target.is_file() and target.read_text(encoding="utf-8") == source:
        return target
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(source, encoding="utf-8")
    os.replace(temporary, target)
    if target.read_text(encoding="utf-8") != source:
        raise RuntimeError(
            f"Failed to synchronize the shared theorem environments: {target}"
        )
    return target
