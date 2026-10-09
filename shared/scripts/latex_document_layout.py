from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DOCUMENT_LAYOUT_SOURCE_PATH = ROOT_DIR / "shared" / "templates" / "document_layout.tex"
DOCUMENT_LAYOUT_FILENAME = "document-layout.tex"
DOCUMENT_LAYOUT_INPUT = rf"\input{{preamble/{DOCUMENT_LAYOUT_FILENAME.removesuffix('.tex')}}}"
CHAPTER_TITLE_FILENAME = "chapter.title.tex"


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
        r"\brokenpenalty=10000",
        r"\clubpenalty=10000",
        r"\widowpenalty=10000",
        r"\displaywidowpenalty=10000",
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


def shared_chapter_title_tex() -> str:
    """Return the single chapter-title contract used by every durable PDF."""

    return r"""\RequirePackage[explicit]{titlesec}
\RequirePackage{xcolor}

% Keep the title text in ordinary TeX flow.  Wrapping \fcolorbox in a
% tikzpicture makes titlesec typeset the argument in nullfont on some XeTeX
% versions, leaving an empty rectangle in the final PDF.
\newcommand{\MPBChapterTitleBox}[1]{%
  \begingroup
  \setlength{\fboxrule}{0.8pt}%
  \setlength{\fboxsep}{0.35cm}%
  \noindent\fcolorbox{BlueMain}{BlueMain!10}{%
    \parbox{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}{%
      \raggedright\color{BlueDark}\bfseries\Huge #1}}%
  \endgroup
}

\titleformat{\chapter}[display]
  {\normalfont}{}{0pt}{\MPBChapterTitleBox{Chapter~\thechapter\quad #1}}
  [\vspace{1.2em}]
\titleformat{name=\chapter,numberless}[display]
  {\normalfont}{}{0pt}{\MPBChapterTitleBox{#1}}
  [\vspace{1.2em}]
\titlespacing*{\chapter}{0pt}{0pt}{1.5em}
"""


def sync_chapter_title(preamble_dir: Path) -> Path:
    """Write the canonical title style into an isolated LaTeX build."""

    preamble_dir.mkdir(parents=True, exist_ok=True)
    target = preamble_dir / CHAPTER_TITLE_FILENAME
    source = shared_chapter_title_tex()
    if target.is_file() and target.read_text(encoding="utf-8") == source:
        return target
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(source, encoding="utf-8")
    os.replace(temporary, target)
    if target.read_text(encoding="utf-8") != source:
        raise RuntimeError(f"Failed to synchronize the shared chapter title: {target}")
    return target


def sync_shared_latex_layout(preamble_dir: Path) -> Path:
    """Synchronize both the prose layout and chapter-title contracts."""

    document_layout = sync_document_layout(preamble_dir)
    sync_chapter_title(preamble_dir)
    return document_layout


def shared_math_prose_rules() -> str:
    """Read the language-independent prose contract from the sole editable rules file."""
    source = (ROOT_DIR / "shared" / "templates" / "latex_writing_rules.txt").read_text(encoding="utf-8")
    start_marker = "BEGIN_SHARED_MATH_PROSE_LAYOUT"
    end_marker = "END_SHARED_MATH_PROSE_LAYOUT"
    if source.count(start_marker) != 1 or source.count(end_marker) != 1:
        raise RuntimeError("Shared math prose layout contract is missing or ambiguous.")
    start = source.index(start_marker)
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end].strip() + "\n"
