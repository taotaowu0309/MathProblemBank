"""Deterministic, optional LaTeX/PDF quality checks.

This module is intentionally a quality sidecar rather than a second compiler.
It never invokes an Agent and never rewrites a complete TeX document.  The
build pipeline decides which findings are eligible for the single safe repair
round.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal, Sequence


QualityLevel = Literal["fast", "standard", "strict"]
QUALITY_LEVELS = ("fast", "standard", "strict")
_GEOMETRY_PROFILES = {
    "default": {"top": 48.0, "bottom": 30.0, "gap": 18.0},
    "compact": {"top": 40.0, "bottom": 24.0, "gap": 14.0},
    "lecture": {"top": 54.0, "bottom": 36.0, "gap": 20.0},
}
LATEX_QUALITY_VERSION = "latex-quality-v1"

_PROTECTED_ENVIRONMENTS = ("verbatim", "Verbatim", "lstlisting", "minted", "tikzpicture")
_PROTECTED_RE = re.compile(
    r"\\begin\{(?P<name>verbatim|Verbatim|lstlisting|minted|tikzpicture)\}.*?"
    r"\\end\{(?P=name)\}",
    re.DOTALL,
)
_CONNECTOR_START_RE = re.compile(
    r"^(?:where|which|that|and|or|but|because|so that|respectively|"
    r"其中|而|并且|因为|从而|所以|即)\b",
    re.IGNORECASE,
)
_CONTINUATION_END_RE = re.compile(
    r"(?:[,;:，；：]|\b(?:and|or|but|because|since|if|when|where|"
    r"which|that|thus|therefore|hence|of|to|with|for|is|are)\s*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QualityFinding:
    path: str
    kind: str
    line: int
    severity: Literal["high", "medium", "low"]
    message: str
    auto_fixable: bool = False
    column: int | None = None


@dataclass(frozen=True)
class SourceQualityReport:
    checked_files: tuple[str, ...]
    findings: tuple[QualityFinding, ...]
    parser_backend: str
    cache_hit: bool = False


@dataclass(frozen=True)
class PdfGeometryFinding:
    page: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float]
    severity: Literal["medium", "low"]
    font: str | None = None
    fontsize: float | None = None
    source_file: str | None = None
    source_line: int | None = None


@dataclass(frozen=True)
class ToolDiagnostic:
    tool: str
    return_code: int
    output: str
    timed_out: bool = False
    available: bool = True


@dataclass(frozen=True)
class SynctexLocation:
    source_file: str
    source_line: int | None
    raw: str


def _protected_spans(source: str) -> tuple[tuple[int, int], ...]:
    return tuple(match.span() for match in _PROTECTED_RE.finditer(source))


def _is_protected(offset: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in spans)


def _parse_backend(source: str) -> str:
    """Parse with pylatexenc when present, without making it a hard dependency."""

    try:
        from pylatexenc.latexwalker import LatexWalker  # type: ignore
    except ImportError:
        return "fallback-protected-scanner"
    try:
        LatexWalker(source, tolerant_parsing=True).get_latex_nodes()
    except Exception:
        return "pylatexenc-tolerant-fallback"
    return "pylatexenc"


def analyze_latex_source(path: Path) -> SourceQualityReport:
    """Report paragraph-boundary risks without changing ``path``.

    A blank-line boundary is only reported when the neighbouring prose strongly
    suggests continuation.  It is medium confidence by policy and therefore
    never directly edits the file.
    """

    path = Path(path).resolve()
    source = path.read_text(encoding="utf-8", errors="replace")
    spans = _protected_spans(source)
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    findings: list[QualityFinding] = []

    for blank_index in range(1, len(lines) - 1):
        if lines[blank_index].strip():
            continue
        previous = lines[blank_index - 1].strip()
        following_index = blank_index + 1
        while following_index < len(lines) and not lines[following_index].strip():
            following_index += 1
        if following_index >= len(lines):
            continue
        boundary_offset = offsets[blank_index]
        if _is_protected(boundary_offset, spans):
            continue
        following = lines[following_index].strip()
        if following.startswith(("%", r"\begin", r"\end", r"\section", r"\subsection")):
            continue
        previous_continues = bool(_CONTINUATION_END_RE.search(previous))
        following_continues = bool(_CONNECTOR_START_RE.match(following))
        if not (previous_continues or following_continues):
            continue
        findings.append(
            QualityFinding(
                path=str(path),
                kind="suspicious_paragraph_break",
                line=following_index + 1,
                severity="medium",
                message=(
                    "空行两侧的正文具有明显句法延续信号；保留为人工复核，"
                    "不会自动合并自然段。"
                ),
                column=1,
            )
        )

    return SourceQualityReport(
        checked_files=(str(path),),
        findings=tuple(findings),
        parser_backend=_parse_backend(source),
    )


def repair_high_confidence_paragraph_breaks(source: str) -> tuple[str, int]:
    """Remove only an unambiguous continuation blank line.

    The preceding prose must end in a continuation punctuation mark and the
    following prose must begin with a known connector.  This is intentionally
    narrower than the historical semantic normalizer and performs a minimal
    source edit; all other paragraph findings remain report-only.
    """

    lines = source.splitlines(keepends=True)
    if len(lines) < 3:
        return source, 0
    spans = _protected_spans(source)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    remove: set[int] = set()
    for blank_index in range(1, len(lines) - 1):
        if lines[blank_index].strip():
            continue
        previous = lines[blank_index - 1].strip()
        next_index = blank_index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines) or _is_protected(offsets[blank_index], spans):
            continue
        following = lines[next_index].strip()
        if following.startswith(("%", r"\begin", r"\end")):
            continue
        if previous.endswith((",", ";", ":", "，", "；", "：")) and _CONNECTOR_START_RE.match(following):
            remove.update(range(blank_index, next_index))
    if not remove:
        return source, 0
    return "".join(line for index, line in enumerate(lines) if index not in remove), len(remove)


def source_token_signature(source: str) -> tuple[str, ...]:
    """Return a whitespace/comment-insensitive token signature for invariants."""

    code_lines = []
    for line in source.splitlines():
        escaped = False
        out: list[str] = []
        for char in line:
            if char == "%" and not escaped:
                break
            out.append(char)
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        code_lines.append("".join(out))
    return tuple(re.findall(r"\\[A-Za-z@]+|\\.|[A-Za-z]+|\d+(?:\.\d+)?|[^\s]", "\n".join(code_lines)))


def source_cache_key(
    paths: Iterable[Path], *, quality_level: QualityLevel, profile: str = "default"
) -> str:
    digest = hashlib.sha256()
    digest.update(f"{LATEX_QUALITY_VERSION}|{quality_level}|{profile}".encode("utf-8"))
    for path in sorted((Path(value).resolve() for value in paths), key=str):
        digest.update(str(path).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_source_report_cache(path: Path, key: str) -> SourceQualityReport | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("key") != key:
            return None
        return SourceQualityReport(
            checked_files=tuple(payload.get("checked_files", ())),
            findings=tuple(QualityFinding(**item) for item in payload.get("findings", ())),
            parser_backend=str(payload.get("parser_backend", "cache")),
            cache_hit=True,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_source_report_cache(path: Path, key: str, report: SourceQualityReport) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"key": key, **asdict(report), "findings": [asdict(item) for item in report.findings]}
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def analyze_sources(
    paths: Iterable[Path],
    *,
    quality_level: QualityLevel,
    profile: str = "default",
    cache_path: Path | None = None,
) -> SourceQualityReport:
    paths = tuple(Path(path).resolve() for path in paths if Path(path).is_file())
    key = source_cache_key(paths, quality_level=quality_level, profile=profile)
    if cache_path is not None:
        cached = load_source_report_cache(cache_path, key)
        if cached is not None:
            return cached
    reports = [analyze_latex_source(path) for path in paths]
    findings = tuple(finding for report in reports for finding in report.findings)
    backends = sorted({report.parser_backend for report in reports})
    report = SourceQualityReport(tuple(str(path) for path in paths), findings, "+".join(backends) or "none")
    if cache_path is not None:
        save_source_report_cache(cache_path, key, report)
    return report


def run_checker(
    executable: str,
    paths: Iterable[Path],
    *,
    cwd: Path,
    timeout: float = 20.0,
) -> ToolDiagnostic:
    command = [executable, *(str(Path(path)) for path in paths)]
    resolved = shutil.which(executable)
    if resolved is None:
        return ToolDiagnostic(executable, -1, "not installed", available=False)
    try:
        completed = subprocess.run(
            [resolved, *command[1:]],
            cwd=Path(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") + (error.stderr or "")
        return ToolDiagnostic(executable, -1, output[-8000:], timed_out=True)
    output = (completed.stdout or "") + (completed.stderr or "")
    return ToolDiagnostic(executable, int(completed.returncode), output[-8000:])


def run_strict_source_tools(paths: Iterable[Path], *, cwd: Path) -> tuple[ToolDiagnostic, ...]:
    """Run bounded optional static tools; diagnostics never rewrite sources."""

    path_tuple = tuple(paths)
    return (
        run_checker("chktex", path_tuple, cwd=cwd),
        run_checker("badness", path_tuple, cwd=cwd),
    )


def inspect_pdf_geometry(
    pdf_path: Path,
    *,
    strict: bool = False,
    profile: str = "default",
) -> list[PdfGeometryFinding]:
    """Perform one conservative PyMuPDF geometry pass."""

    try:
        import fitz  # type: ignore
    except ImportError:
        return []
    settings = _GEOMETRY_PROFILES.get(profile, _GEOMETRY_PROFILES["default"])
    findings: list[PdfGeometryFinding] = []
    document = fitz.open(str(pdf_path))
    try:
        for page_number, page in enumerate(document, start=1):
            height = float(page.rect.height)
            width = float(page.rect.width)
            page_dict = page.get_text("dict")
            previous_bottom: float | None = None
            previous_height: float | None = None
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = block.get("lines", [])
                block_previous_signature: tuple[str, float] | None = None
                block_previous_text = ""
                for line in lines:
                    spans = line.get("spans", [])
                    text = "".join(str(span.get("text", "")) for span in spans).strip()
                    if not text or not spans:
                        continue
                    x0, y0, x1, y1 = (float(value) for value in line.get("bbox", (0, 0, 0, 0)))
                    if y0 < settings["top"] or y1 > height - settings["bottom"]:
                        continue
                    span = spans[0]
                    font = str(span.get("font", "")) or None
                    fontsize = float(span.get("size", 0.0) or 0.0)
                    if x1 > width + 0.5:
                        findings.append(PdfGeometryFinding(page_number, "text_edge_overflow", text, (x0, y0, x1, y1), "medium", font, fontsize))
                    if previous_bottom is not None and previous_height:
                        gap = y0 - previous_bottom
                        if gap > max(settings["gap"], previous_height * 2.4) and not strict:
                            findings.append(PdfGeometryFinding(page_number, "abnormal_vertical_gap", text, (x0, y0, x1, y1), "low", font, fontsize))
                        elif gap > max(settings["gap"], previous_height * 2.4) and strict:
                            findings.append(PdfGeometryFinding(page_number, "abnormal_vertical_gap", text, (x0, y0, x1, y1), "medium", font, fontsize))
                    signature = (font or "", round(fontsize, 1))
                    if (
                        block_previous_signature
                        and len(block_previous_text) >= 24
                        and len(text) >= 24
                        and signature != block_previous_signature
                        and abs(signature[1] - block_previous_signature[1]) >= 4.0
                    ):
                        findings.append(PdfGeometryFinding(page_number, "abrupt_font_change", text, (x0, y0, x1, y1), "low", font, fontsize))
                    previous_bottom = y1
                    previous_height = max(1.0, y1 - y0)
                    block_previous_signature = signature
                    block_previous_text = text
    finally:
        document.close()
    return findings


def map_pdf_point_with_synctex(
    pdf_path: Path,
    *,
    page: int,
    x: float,
    y: float,
    synctex_executable: str | None = None,
    timeout: float = 5.0,
) -> SynctexLocation | None:
    executable = synctex_executable or shutil.which("synctex")
    if not executable:
        return None
    query = f"{page}:{x:.2f}:{y:.2f}:{Path(pdf_path).resolve()}"
    try:
        completed = subprocess.run(
            [executable, "edit", "-o", query],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = (completed.stdout or "") + (completed.stderr or "")
    input_match = re.search(r"Input:\s*(.+)", raw)
    line_match = re.search(r"Line:\s*(\d+)", raw)
    if not input_match:
        return None
    return SynctexLocation(input_match.group(1).strip().strip('"'), int(line_match.group(1)) if line_match else None, raw)


__all__ = [
    "LATEX_QUALITY_VERSION",
    "QUALITY_LEVELS",
    "PdfGeometryFinding",
    "QualityFinding",
    "QualityLevel",
    "SourceQualityReport",
    "SynctexLocation",
    "ToolDiagnostic",
    "analyze_latex_source",
    "analyze_sources",
    "inspect_pdf_geometry",
    "map_pdf_point_with_synctex",
    "repair_high_confidence_paragraph_breaks",
    "run_strict_source_tools",
    "save_source_report_cache",
    "source_cache_key",
    "source_token_signature",
]
