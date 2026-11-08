"""LaTeX of a formula in a PDF region, from the TeX source or a MinerU parse.

Project lectures are compiled with ``-synctex=1``: a PDF point maps to a line of
the ``.tex`` source, and the display environment or inline math around that line
is offered for copying.  Textbooks the project parsed with MinerU have a
``MPB_MERGED_content_list.json`` whose equation blocks carry LaTeX; the blocks
under the region are offered instead, marked as recognised text to check.
Nothing here runs a model; MinerU output is only read from its existing cache.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from shared.scripts.latex_quality import map_pdf_point_with_synctex

Rect = tuple[float, float, float, float]

DISPLAY_ENVIRONMENTS = (
    "equation", "align", "gather", "multline", "eqnarray", "flalign", "alignat", "displaymath",
    "split", "aligned", "gathered", "cases", "CD",
)
_INLINE_RE = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$|(?<!\\)\$(.+?)(?<!\\)\$|\\\((.+?)\\\)", re.DOTALL)


@dataclass
class LatexCandidate:
    label: str
    latex: str


@dataclass
class LatexResult:
    source_note: str
    candidates: list[LatexCandidate] = field(default_factory=list)


# -- SyncTeX (project lectures) ------------------------------------------------------------


def synctex_executable() -> str | None:
    found = shutil.which("synctex")
    if found:
        return found
    for candidate in (Path("/Library/TeX/texbin/synctex"), Path("/usr/local/texlive/bin/synctex")):
        if candidate.is_file():
            return str(candidate)
    return None


def map_synctex_input(recorded: str, pdf_path: Path) -> Path | None:
    """A source path recorded by SyncTeX on either machine, found again next to this PDF.

    The PDF may have been compiled on the other computer (a drive path such as ``D:/Temp/...``)
    or in a build folder; the longest path suffix that exists under the PDF's folder
    (or its ``latex`` folder) is the source.
    """

    parts = [part for part in re.split(r"[\\/]+", str(recorded).strip().strip('"')) if part and part != "."]
    if not parts:
        return None
    base = Path(pdf_path).resolve().parent
    roots = [base, base / "latex", base.parent]
    for start in range(len(parts)):
        suffix = Path(*parts[start:])
        for root in roots:
            candidate = root / suffix
            if candidate.is_file():
                return candidate
    return None


def _strip_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*$", "", line)


_TOKEN_RE = re.compile(r"\\begin\{([A-Za-z]+\*?)\}|\\end\{([A-Za-z]+\*?)\}|\\\[|\\\]|\$\$")


def display_spans(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Every display-math block of a source file as (first, last) line, read from the top.

    Reading forward is the only way to tell an opening ``$$`` from a closing one;
    environments nested in a display (``aligned`` in ``equation``) stay inside it.
    """

    spans: list[tuple[int, int]] = []
    opener: tuple[str, str, int] | None = None  # (kind, environment name, first line)
    depth = 0
    for number, raw in enumerate(lines):
        for match in _TOKEN_RE.finditer(_strip_comment(raw)):
            token = match.group(0)
            begin, finish = match.group(1), match.group(2)
            if opener is None:
                if begin and begin.rstrip("*") in DISPLAY_ENVIRONMENTS:
                    opener, depth = ("env", begin, number), 1
                elif token == "\\[":
                    opener = ("bracket", "", number)
                elif token == "$$":
                    opener = ("dollars", "", number)
                continue
            kind, name, first = opener
            if kind == "env":
                if begin == name:
                    depth += 1
                elif finish == name:
                    depth -= 1
                    if depth == 0:
                        spans.append((first, number))
                        opener = None
            elif (kind == "bracket" and token == "\\]") or (kind == "dollars" and token == "$$"):
                spans.append((first, number))
                opener = None
    return spans


def display_environment(lines: Sequence[str], index: int) -> tuple[int, int] | None:
    """The display-math block containing line ``index`` (0-based), as (first, last) lines."""

    for first, last in display_spans(lines):
        if first <= index <= last:
            return first, last
        if first > index:
            break
    return None


def inline_math(text: str) -> list[str]:
    segments = []
    for match in _INLINE_RE.finditer(text):
        whole = match.group(0).strip()
        if len(whole) > 2:
            segments.append(whole)
    return segments


def latex_from_source(lines: Sequence[str], first: int, last: int) -> list[LatexCandidate]:
    """Candidates for source lines ``first..last`` (0-based): display block, inline formulas, raw lines."""

    first, last = max(0, min(first, last)), min(len(lines) - 1, max(first, last))
    candidates: list[LatexCandidate] = []
    seen: set[str] = set()

    def add(label: str, latex: str) -> None:
        latex = latex.strip()
        if latex and latex not in seen:
            seen.add(latex)
            candidates.append(LatexCandidate(label, latex))

    for block in display_spans(lines):
        if block[1] >= first and block[0] <= last:
            add(f"行间公式（第 {block[0] + 1}–{block[1] + 1} 行）", "\n".join(lines[block[0]:block[1] + 1]))
        elif block[0] > last:
            break
    excerpt = "\n".join(_strip_comment(line) for line in lines[first:last + 1])
    for number, segment in enumerate(inline_math(excerpt), start=1):
        add(f"行内公式 {number}", segment)
    add(f"源码第 {first + 1}–{last + 1} 行" if last > first else f"源码第 {first + 1} 行", "\n".join(lines[first:last + 1]))
    return candidates


def synctex_latex(pdf_path: Path, page_index: int, rect: Rect) -> LatexResult | None:
    pdf_path = Path(pdf_path)
    if not pdf_path.with_suffix(".synctex.gz").is_file() and not pdf_path.with_suffix(".synctex").is_file():
        return None
    executable = synctex_executable()
    if executable is None:
        return None
    x0, y0, x1, y1 = rect
    points = [((x0 + x1) / 2, (y0 + y1) / 2), (x0 + 4, y0 + 4), (x1 - 4, y1 - 4)]
    hits: list[tuple[Path, int]] = []
    for x, y in points:
        location = map_pdf_point_with_synctex(pdf_path, page=page_index + 1, x=x, y=y, synctex_executable=executable)
        if location is None or not location.source_line:
            continue
        source = map_synctex_input(location.source_file, pdf_path)
        if source is not None:
            hits.append((source, int(location.source_line)))
    if not hits:
        return None
    source = hits[0][0]
    numbers = [line for path, line in hits if path == source]
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    first, last = min(numbers) - 1, max(numbers) - 1
    try:
        shown = source.relative_to(pdf_path.resolve().parent)
    except ValueError:
        shown = source.name
    return LatexResult(f"来源：TeX 源文件 {shown} 第 {first + 1} 行附近（SyncTeX 定位）", latex_from_source(lines, first, last))


# -- MinerU (parsed textbooks) --------------------------------------------------------------


def mineru_content_lists(project_root: Path, source_sha256: str) -> list[Path]:
    roots = [
        *Path(project_root).glob(f"online_courses/projects/*/*/*/reference_materials/mineru_cache/{source_sha256}"),
        *Path(project_root).glob(f"content/translations/*/mineru_cache/{source_sha256}"),
    ]
    found = [path for root in roots for path in root.glob("*/MPB_MERGED_content_list.json")]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def _overlap(first: Rect, second: Rect) -> float:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    return max(0.0, width) * max(0.0, height)


def latex_from_mineru_items(items: Sequence[dict[str, Any]], page_index: int, rect: Rect, page_size: tuple[float, float]) -> list[LatexCandidate]:
    width, height = page_size
    scored: list[tuple[float, float, dict[str, Any]]] = []
    for item in items:
        if int(item.get("page_idx") or -1) != page_index:
            continue
        box = item.get("bbox") or []
        if len(box) < 4:
            continue
        block = (width * float(box[0]) / 1000, height * float(box[1]) / 1000, width * float(box[2]) / 1000, height * float(box[3]) / 1000)
        area = _overlap(block, rect)
        if area > 0:
            scored.append((float(block[1]), area, item))
    candidates: list[LatexCandidate] = []
    seen: set[str] = set()
    for _top, _area, item in sorted(scored, key=lambda entry: entry[0]):
        text = str(item.get("text") or "").strip()
        kind = str(item.get("type") or "")
        if not text:
            continue
        if kind == "equation":
            latex = text
            if latex.startswith("$$") and latex.endswith("$$"):
                latex = "\\[" + latex[2:-2].strip("\n") + "\\]"
            if latex not in seen:
                seen.add(latex)
                candidates.append(LatexCandidate("行间公式（MinerU 识别）", latex))
        elif kind == "text":
            for number, segment in enumerate(inline_math(text), start=1):
                if segment not in seen:
                    seen.add(segment)
                    candidates.append(LatexCandidate(f"行内公式 {number}（MinerU 识别）", segment))
            if text not in seen:
                seen.add(text)
                candidates.append(LatexCandidate("整段文字（MinerU 识别）", text))
    return candidates


def mineru_latex(pdf_path: Path, page_index: int, rect: Rect, *, source_sha256: str, project_root: Path, page_size: tuple[float, float]) -> LatexResult | None:
    for content_list in mineru_content_lists(project_root, source_sha256):
        try:
            items = json.loads(content_list.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        candidates = latex_from_mineru_items(items, page_index, rect, page_size)
        if candidates:
            return LatexResult("来源：MinerU 对这本教材的解析结果。这是识别出的文字，复制前请对照原文核对。", candidates)
    return None


def formula_latex(
    pdf_path: Path,
    page_index: int,
    rect: Rect,
    *,
    source_sha256: str,
    project_root: Path,
    page_size: tuple[float, float],
) -> LatexResult | None:
    return synctex_latex(pdf_path, page_index, rect) or mineru_latex(
        pdf_path, page_index, rect, source_sha256=source_sha256, project_root=project_root, page_size=page_size,
    )


__all__ = [
    "LatexCandidate", "LatexResult", "display_environment", "formula_latex", "inline_math",
    "latex_from_mineru_items", "latex_from_source", "map_synctex_input", "mineru_latex", "synctex_latex",
]
