from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DOCUMENT_LAYOUT_SOURCE_PATH = ROOT_DIR / "shared" / "templates" / "document_layout.tex"
DOCUMENT_LAYOUT_FILENAME = "document-layout.tex"
DOCUMENT_LAYOUT_INPUT = rf"\input{{preamble/{DOCUMENT_LAYOUT_FILENAME.removesuffix('.tex')}}}"


def shared_document_layout_tex() -> str:
    source = DOCUMENT_LAYOUT_SOURCE_PATH.read_text(encoding="utf-8")
    required_markers = (
        r"\RequirePackage{enumitem}",
        r"\setlength{\parindent}{2em}",
        r"\setlist[enumerate,1]",
        "wide=0pt",
        "listparindent=2em",
        r"parsep=0.35\baselineskip",
        r"itemsep=0.75\baselineskip",
        r"topsep=0.5\baselineskip",
        r"\newcommand{\MPBSolutionHeading}",
    )
    missing = [marker for marker in required_markers if marker not in source]
    if missing:
        raise RuntimeError(
            "The shared document layout is incomplete: " + ", ".join(missing)
        )
    return source.rstrip() + "\n"


def sync_document_layout(preamble_dir: Path) -> Path:
    preamble_dir.mkdir(parents=True, exist_ok=True)
    target = preamble_dir / DOCUMENT_LAYOUT_FILENAME
    source = shared_document_layout_tex()
    if target.is_file() and target.read_text(encoding="utf-8") == source:
        return target
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(source, encoding="utf-8")
    os.replace(temporary, target)
    if target.read_text(encoding="utf-8") != source:
        raise RuntimeError(f"Failed to synchronize the shared document layout: {target}")
    return target
