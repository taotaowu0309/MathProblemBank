"""Conservative detection and repair of over-wide LaTeX display formulas.

Only long, plain display formulas with at least two top-level relation
operators are rewritten. Existing alignment, array, matrix, cases, and other
structured math are left untouched. The final PDF/log remains authoritative.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_DISPLAY_RE = re.compile(r"(?P<open>\$\$|\\\[)(?P<body>.*?)(?P<close>\$\$|\\\])", re.DOTALL)
_OVERFULL_RE = re.compile(r"Overfull \\hbox \((?P<points>[0-9.]+)pt too wide\)(?P<context>[^\n]*)", re.I)
_LINE_RE = re.compile(r"(?:at lines?\s+(?P<start>[0-9]+)(?:--(?P<end>[0-9]+))?|at line\s+(?P<single>[0-9]+))", re.I)
_RELATIONS = (r"\Longleftrightarrow", r"\Leftrightarrow", r"\Longrightarrow", r"\Rightarrow", r"\longrightarrow", r"\mapsto", r"\implies", r"\iff", r"\to", "=")
_STRUCTURED = ("\\begin{aligned}", "\\begin{split}", "\\begin{array", "\\begin{gather", "\\begin{multline", "\\begin{matrix", "\\begin{pmatrix", "\\begin{bmatrix", "\\begin{cases", "\\begin{smallmatrix")

@dataclass(frozen=True)
class OverfullBox:
    points: float
    start_line: int | None
    end_line: int | None
    raw: str

@dataclass(frozen=True)
class Repair:
    start: int
    end: int
    before: str
    after: str
    relation_count: int


# These are the characters which must remain attached to the preceding or
# following text in Chinese and English prose.  The source checker is only a
# conservative preflight: TeX source newlines are not PDF line breaks, so a
# finding is emitted only for an explicit source-line boundary.
_LINE_START_PUNCTUATION = "，。；：！？、）》】』」〕〉》’”％%.,;:!?)]"
_LINE_END_OPENING = "（【《「『〔〈“‘([{<"
_MATH_RELATION_START = re.compile(r"^(?:=|\\(?:le|ge|ne|neq|approx|sim|to|rightarrow|Longrightarrow|iff|implies))(?:\s|$)")


@dataclass(frozen=True)
class LayoutFinding:
    """A deterministic source-level layout issue and its safe repair hint."""

    kind: str
    line: int
    message: str
    before: str
    after: str | None = None


@dataclass(frozen=True)
class PdfLayoutFinding:
    page: int
    block: int
    line: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float]


_SEMANTIC_DISPLAY_CONNECTOR_START = re.compile(
    r"^(?:\b(?:because|where|which|and|or|but|as|while|that|namely)|"
    r"因为|其中|而|并且|即|从而|所以|这说明)",
    re.I,
)
_SEMANTIC_DISPLAY_CONNECTOR_END = re.compile(
    r"(?:[,;:，；：]|\b(?:and|or|but|because|since|as|if|when|where|"
    r"that|which|then|thus|hence|therefore|consequently|consider|"
    r"assume|let|suppose|such as|equal to|is|are|was|were|be|has|"
    r"have|of|to|from|with|for|in|on|at|namely|restriction|"
    r"conclusion|follows|implies)\s*)$",
    re.I,
)
_SEMANTIC_INLINE_PREFIX_END = re.compile(
    r"(?:\b(?:thus|then|hence|therefore|since|let|consider)\s*)$",
    re.I,
)


def _is_standalone_display_block(block: str) -> bool:
    value = block.strip()
    if not value:
        return False
    if value.startswith("$$") and value.endswith("$$"):
        return True
    if value.startswith(r"\[") and value.endswith(r"\]"):
        return True
    return bool(
        re.match(
            r"^\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}",
            value,
        )
        and re.search(r"\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}$", value)
    )


def _block_ends_with_display(block: str) -> bool:
    value = block.strip()
    return bool(
        re.search(
            r"(?:\$\$.*\$\$|\\\[.*?\\\]|"
            r"\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}"
            r".*?\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\})\s*$",
            value,
            re.DOTALL,
        )
    )


def _short_display_to_inline(block: str) -> str | None:
    value = block.strip()
    body: str | None = None
    if value.startswith("$$") and value.endswith("$$"):
        body = value[2:-2]
    elif value.startswith(r"\[") and value.endswith(r"\]"):
        body = value[2:-2]
    if body is None:
        return None
    body = re.sub(r"\s+", " ", body).strip()
    if (
        not body
        or len(body) > 78
        or body.count("=") >= 2
        or r"\qquad" in body
        or "$$" in body
        or r"\[" in body
        or r"\]" in body
        or r"\begin" in body
        or r"\end" in body
        or r"\tag" in body
        or r"\label" in body
        or r"\\" in body
    ):
        return None
    return rf"\({body}\)"


def _replace_short_display_suffix(block: str) -> str | None:
    value = block.rstrip()
    match = re.search(r"(?:\$\$.*\$\$|\\\[.*?\\\])$", value, re.DOTALL)
    if match is None:
        return None
    inline = _short_display_to_inline(match.group(0))
    if inline is None:
        return None
    prefix = value[: match.start()].rstrip()
    normalized_prefix = re.sub(r"\s+", " ", prefix).strip()
    if (
        len(normalized_prefix) + len(inline) > 125
        or normalized_prefix.endswith((":", "："))
    ):
        return None
    return (prefix + "\n" if prefix else "") + inline


def _replace_embedded_short_displays(block: str) -> tuple[str, bool]:
    """Inline short displays embedded inside an ordinary prose block.

    A generated answer often contains ``Since``/``Thus`` followed by a short
    ``$$...$$`` and then continues the same sentence.  Treating that display
    as a paragraph boundary creates a visually isolated formula even though
    the source sentence is continuous.  Only convert a display when the text
    immediately before it is short enough to share a line, or when the text
    immediately after it is an obvious lowercase continuation.
    """
    value = block
    matches = list(_DISPLAY_RE.finditer(block))
    if len(matches) != 1:
        return block, False
    replacements: list[tuple[int, int, str]] = []
    for match in matches:
        inline = _short_display_to_inline(match.group(0))
        if inline is None:
            continue
        prefix = re.sub(r"\s+", " ", block[:match.start()]).strip()
        suffix = re.sub(r"\s+", " ", block[match.end():]).strip()
        prefix_is_safe = bool(prefix) and len(prefix) + len(inline) <= 125 and not prefix.endswith((":", "："))
        if (
            prefix
            and _SEMANTIC_INLINE_PREFIX_END.search(prefix)
            and len(inline) <= 90
            and not prefix.endswith((":", "："))
        ):
            prefix_is_safe = True
        suffix_is_continuation = bool(suffix) and (
            suffix[:1].islower() or _SEMANTIC_DISPLAY_CONNECTOR_START.match(suffix)
        )
        if not prefix_is_safe and not suffix_is_continuation:
            continue
        if prefix and _SEMANTIC_INLINE_PREFIX_END.search(prefix):
            # Replace the source whitespace before the display with a
            # nonbreaking space.  A standalone \nobreak after that whitespace
            # is too late: TeX may still break at the preceding glue.
            before = re.sub(r"\s+$", "~", block[: match.start()])
            return before + inline + block[match.end() :], True
        replacements.append((match.start(), match.end(), inline))
    if not replacements:
        return block, False
    output: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        output.append(block[cursor:start])
        output.append(replacement)
        cursor = end
    output.append(block[cursor:])
    return "".join(output), True


def _suppress_indent_after_structured_display(block: str) -> tuple[str, bool]:
    """Keep lowercase sentence continuations flush after a display array."""
    for match in _DISPLAY_RE.finditer(block):
        display = match.group(0)
        if not any(marker in display for marker in _STRUCTURED):
            continue
        tail = block[match.end():]
        continuation = re.match(
            r"(?P<space>\s+)(?P<text>(?:because|where|which|and|or|but|因为|其中|而|并且|即)\b.*)",
            tail,
            re.IGNORECASE | re.DOTALL,
        )
        if continuation is None:
            continue
        replacement = (
            block[: match.end()]
            + continuation.group("space")
            + r"\noindent "
            + continuation.group("text")
        )
        return replacement, True
    return block, False


def _is_plain_prose_block(block: str) -> bool:
    value = block.strip()
    return bool(value) and not value.startswith(("%", r"\begin", r"\end", r"\chapter", r"\section"))


def normalize_latex_semantic_paragraphs(source: str) -> str:
    r"""Keep connected prose and its display formula in one TeX paragraph.

    Imported problem-set and lecture answers frequently put a blank line
    before and after every display formula.  That makes TeX indent the
    continuation as a new paragraph, producing fragments such as a lone
    ``because ...`` after an aligned equation.  Only unambiguous grammatical
    connectors are joined; genuine paragraph boundaries and translation
    paragraph order remain untouched by callers that do not opt in.
    """
    parts = re.split(r"(\n[ \t]*\n+)", str(source or ""))
    blocks = parts[0::2]
    separators = parts[1::2]
    if not blocks:
        return source

    join_boundaries: set[int] = set()
    semantic_changed = False
    for index, block in enumerate(blocks):
        indent_repaired, indent_changed = _suppress_indent_after_structured_display(block)
        if indent_changed:
            semantic_changed = True
            blocks[index] = indent_repaired
            block = indent_repaired
        embedded_repaired, embedded_changed = _replace_embedded_short_displays(block)
        if embedded_changed:
            semantic_changed = True
            blocks[index] = embedded_repaired
            block = embedded_repaired
            if index + 1 < len(blocks):
                following = re.sub(r"\s+", " ", blocks[index + 1]).strip()
                if _is_plain_prose_block(following) and (
                    following[:1].islower()
                    or _SEMANTIC_DISPLAY_CONNECTOR_START.match(following)
                ):
                    join_boundaries.add(index)
        suffix_repaired = _replace_short_display_suffix(block)
        if suffix_repaired is not None and suffix_repaired != block.strip():
            standalone_suffix = _is_standalone_display_block(block)
            blocks[index] = suffix_repaired
            if standalone_suffix and index > 0 and _is_plain_prose_block(blocks[index - 1]):
                join_boundaries.add(index - 1)
            if index + 1 < len(blocks):
                following = re.sub(r"\s+", " ", blocks[index + 1]).strip()
                if _is_plain_prose_block(following) and (
                    following[:1].islower()
                    or _SEMANTIC_DISPLAY_CONNECTOR_START.match(following)
                ):
                    join_boundaries.add(index)
            continue
        inline = _short_display_to_inline(block)
        if inline is not None:
            previous_is_short_prose = (
                index > 0
                and _is_plain_prose_block(blocks[index - 1])
                and len(re.sub(r"\s+", " ", blocks[index - 1]).strip()) + len(inline) <= 125
                and not re.sub(r"\s+", " ", blocks[index - 1]).strip().endswith((":", "："))
            )
            following = (
                re.sub(r"\s+", " ", blocks[index + 1]).strip()
                if index + 1 < len(blocks)
                else ""
            )
            following_is_continuation = (
                _is_plain_prose_block(following)
                and (
                    following[:1].islower()
                    or _SEMANTIC_DISPLAY_CONNECTOR_START.match(following)
                )
            )
            if not previous_is_short_prose and not following_is_continuation:
                continue
            blocks[index] = inline
            if previous_is_short_prose:
                join_boundaries.add(index - 1)
            if index + 1 < len(blocks):
                if following_is_continuation:
                    join_boundaries.add(index)
            continue
        standalone_display = _is_standalone_display_block(block)
        if (
            not (standalone_display or _block_ends_with_display(block))
            or index + 1 >= len(blocks)
        ):
            continue
        following = re.sub(r"\s+", " ", blocks[index + 1]).strip()
        if standalone_display and index > 0:
            previous = re.sub(r"\s+", " ", blocks[index - 1]).strip()
            if _is_plain_prose_block(previous) and _SEMANTIC_DISPLAY_CONNECTOR_END.search(previous):
                join_boundaries.add(index - 1)
        if _is_plain_prose_block(following) and (
            following[:1].islower()
            or _SEMANTIC_DISPLAY_CONNECTOR_START.match(following)
        ):
            join_boundaries.add(index)

    if not join_boundaries and not semantic_changed:
        return source
    for index in join_boundaries:
        separators[index] = "\n"
    output = blocks[0]
    for separator, block in zip(separators, blocks[1:]):
        output += separator + block
    return output


def inspect_pdf_layout(pdf_path: Path) -> list[PdfLayoutFinding]:
    """Inspect rendered PDF text lines for orphan punctuation/delimiters.

    PyMuPDF's line objects are used instead of plain ``pdftotext`` so the
    result includes page and geometry.  Header/footer furniture is ignored by
    a conservative vertical margin.  This is a review gate: source repairs
    still require a recompile because visual line breaks have no one-to-one
    relationship with source newlines.
    """
    try:
        import fitz  # type: ignore
    except Exception:
        return []
    findings: list[PdfLayoutFinding] = []
    document = fitz.open(str(pdf_path))
    try:
        for page_number, page in enumerate(document, start=1):
            page_height = float(page.rect.height)
            page_dict = page.get_text("dict")
            for block_number, block in enumerate(page_dict.get("blocks", [])):
                if block.get("type") != 0:
                    continue
                for line_number, line in enumerate(block.get("lines", []), start=1):
                    spans = line.get("spans", [])
                    text = "".join(str(span.get("text", "")) for span in spans).strip()
                    if not text or not spans:
                        continue
                    bbox = tuple(float(value) for value in line.get("bbox", (0, 0, 0, 0)))
                    if bbox[1] < 48 or bbox[3] > page_height - 30:
                        continue
                    first = text[0]
                    last = text[-1]
                    if bbox[0] <= 130 and first in _LINE_START_PUNCTUATION:
                        findings.append(PdfLayoutFinding(page_number, block_number, line_number, "line_start_punctuation", text, bbox))
                    if bbox[2] >= float(page.rect.width) - 80 and last in _LINE_END_OPENING:
                        findings.append(PdfLayoutFinding(page_number, block_number, line_number, "line_end_opening_delimiter", text, bbox))
                    if bbox[0] <= 130 and (first in "=≤≥≠≈→⇒↔+-−" or text.startswith((r"\le", r"\ge", r"\to"))):
                        findings.append(PdfLayoutFinding(page_number, block_number, line_number, "line_start_relation", text, bbox))
    finally:
        document.close()
    return findings


def _code_line(line: str) -> str:
    """Remove comments while preserving escaped percent signs."""
    out: list[str] = []
    escaped = False
    for char in line:
        if char == "%" and not escaped:
            break
        out.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return "".join(out)


def inspect_latex_layout(source: str) -> list[LayoutFinding]:
    """Find explicit line-boundary violations from the shared prose contract.

    This intentionally skips comments, LaTeX command lines, and rows inside
    alignment/table environments.  It catches the common source form which
    later becomes a visible orphan punctuation mark, while avoiding broad
    rewrites of mathematically meaningful source formatting.
    """
    findings: list[LayoutFinding] = []
    lines = source.splitlines()
    env_depth = 0
    display_math = False
    previous_index: int | None = None
    for index, raw in enumerate(lines, start=1):
        line = _code_line(raw)
        stripped = line.strip()
        if re.search(r"(?:\\\[|\$\$)", stripped) and not display_math:
            display_math = True
            if re.search(r"(?:\\\]|\$\$)\s*$", stripped):
                display_math = False
            previous_index = index if stripped else previous_index
            continue
        if display_math:
            if re.search(r"(?:\\\]|\$\$)\s*$", stripped):
                display_math = False
            previous_index = index if stripped else previous_index
            continue
        begin_names = re.findall(r"\\begin\{([^{}]+)\}", line)
        end_names = re.findall(r"\\end\{([^{}]+)\}", line)
        structured = any(
            name in {"aligned", "alignedat", "array", "cases", "matrix", "pmatrix", "bmatrix", "gather", "split", "multline", "equation", "equation*", "align", "align*", "tikzpicture", "tikzcd"}
            for name in begin_names + end_names
        )
        if structured:
            env_depth += len(begin_names) - len(end_names)
            previous_index = index if stripped else previous_index
            continue
        if not stripped or stripped.startswith("%"):
            continue
        if env_depth > 0 or stripped.startswith(("\\", "&")):
            previous_index = index
            continue
        if stripped[0] in _LINE_START_PUNCTUATION and previous_index is not None:
            findings.append(LayoutFinding(
                "line_start_punctuation",
                index,
                "标点符号不应出现在新行开头，应与前面的文字或公式绑定。",
                raw,
            ))
        if stripped[-1] in _LINE_END_OPENING:
            findings.append(LayoutFinding(
                "line_end_opening_delimiter",
                index,
                "左括号或左引号不应孤立在行尾，应与后续内容保持粘连。",
                raw,
            ))
        if re.search(r"(?:\\\\|\\newline|\\noindent)\s*$", stripped):
            findings.append(LayoutFinding(
                "manual_paragraph_break",
                index,
                "不要用 \\\\, \\newline 或重复 \\noindent 模拟自然段。",
                raw,
            ))
        if _MATH_RELATION_START.match(stripped) and not stripped.startswith("\\begin"):
            findings.append(LayoutFinding(
                "line_start_relation",
                index,
                "等号、比较符或箭头不应脱离等式链出现在新行开头；长链应使用 aligned。",
                raw,
            ))
        previous_index = index
    return findings


def repair_latex_layout_source(source: str) -> tuple[str, list[LayoutFinding]]:
    """Apply only safe source-level punctuation repairs.

    A leading punctuation mark is joined to the preceding source line with
    ``\\nobreak``.  This preserves the mathematical text and prevents TeX from
    making a line break immediately before the mark.  Other findings are
    reported for human/layout review and are deliberately left unchanged.
    """
    lines = source.splitlines(keepends=True)
    findings = inspect_latex_layout(source)
    repair_lines: set[int] = set()
    for finding in findings:
        if finding.kind != "line_start_punctuation" or finding.line <= 1:
            continue
        prior = _code_line(lines[finding.line - 2]).rstrip()
        if not prior.strip():
            continue
        # A display close or environment boundary is a semantic paragraph
        # boundary; do not glue punctuation onto it automatically.
        if prior.endswith((r"\]", r"\)", r"\end{aligned}", r"\end{equation}", r"\end{align*}")):
            continue
        if prior.lstrip().startswith("\\"):
            continue
        repair_lines.add(finding.line)
    if not repair_lines:
        return source, findings
    output: list[str] = []
    for index, line in enumerate(lines, start=1):
        if index in repair_lines and output:
            leading = line.lstrip(" \t").rstrip("\r\n")
            prior = output[-1].rstrip("\r\n")
            output[-1] = prior + r"\nobreak" + leading
            if line.endswith("\n"):
                output[-1] += "\n"
        else:
            output.append(line)
    repaired = "".join(output)
    return repaired, findings

def detect_overfull_hboxes(log_text: str) -> list[OverfullBox]:
    boxes=[]
    for m in _OVERFULL_RE.finditer(log_text):
        lm=_LINE_RE.search(m.group("context"))
        start=(lm.group("start") or lm.group("single")) if lm else None
        end=(lm.group("end") or start) if lm else None
        boxes.append(OverfullBox(float(m.group("points")), int(start) if start else None, int(end) if end else None, m.group(0)))
    return boxes

def _relation_positions(body: str) -> list[tuple[int,str]]:
    out=[]; curly=paren=bracket=0; i=0
    while i < len(body):
        if body[i]=="\\":
            token=next((x for x in _RELATIONS if body.startswith(x,i)),None)
            if token:
                if not (curly or paren or bracket): out.append((i,token))
                i += len(token); continue
            i += 1
            while i<len(body) and body[i].isalpha(): i+=1
            continue
        ch=body[i]
        if ch=="{": curly+=1
        elif ch=="}" and curly: curly-=1
        elif ch=="(": paren+=1
        elif ch==")" and paren: paren-=1
        elif ch=="[": bracket+=1
        elif ch=="]" and bracket: bracket-=1
        elif ch=="=" and not (curly or paren or bracket): out.append((i,ch))
        i+=1
    return out

def _aligned_body(body: str, relations: list[tuple[int,str]]) -> str | None:
    if len(relations)<2 or any(marker in body for marker in _STRUCTURED): return None
    lines=[]
    for i,(pos,op) in enumerate(relations):
        end=relations[i+1][0] if i+1<len(relations) else len(body)
        rhs=body[pos+len(op):end].strip()
        if not rhs: return None
        if i==0: lines.append(f"{body[:pos].strip()} &{op} {rhs}")
        else: lines.append(f"&{op} {rhs}")
    return " \\\\\n".join(lines)

def repair_latex_source(source: str, *, min_chars: int=110, max_lines: int=8, target_lines: Iterable[int | tuple[int, int]] | None = None) -> tuple[str,list[Repair]]:
    repairs=[]; chunks=[]; cursor=0
    normalized_targets = tuple((line, line) if isinstance(line, int) else line for line in (target_lines or ()))
    for m in _DISPLAY_RE.finditer(source):
        chunks.append(source[cursor:m.start()]); opening,body,closing=m.group("open","body","close"); replacement=m.group(0)
        if (opening == "$$" and closing != "$$") or (opening == r"\[" and closing != r"\]"):
            chunks.append(replacement); cursor=m.end(); continue
        rel=_relation_positions(body)
        block_start = source.count("\n", 0, m.start()) + 1
        block_end = source.count("\n", 0, m.end()) + 1
        targeted = not normalized_targets or any(block_start <= end and block_end >= start for start, end in normalized_targets)
        if targeted and len(body.strip())>=min_chars:
            aligned=_aligned_body(body,rel)
            if aligned is not None and aligned.count(r"\\")+1<=max_lines:
                replacement=f"{opening}\\begin{{aligned}}\n{aligned}\n\\end{{aligned}}{closing}"
                repairs.append(Repair(m.start(),m.end(),m.group(0),replacement,len(rel)))
        chunks.append(replacement); cursor=m.end()
    chunks.append(source[cursor:]); return "".join(chunks),repairs

def repair_file(path: Path, *, output: Path|None=None, min_chars: int=110) -> list[Repair]:
    source=path.read_text(encoding="utf-8"); repaired,repairs=repair_latex_source(source,min_chars=min_chars)
    if repairs: (output or path).write_text(repaired,encoding="utf-8")
    return repairs

def diagnostic_targets(log_text: str, latex_dir: Path) -> list[Path]:
    """Map diagnostics to the most recent source file reported by XeLaTeX."""
    targets=[]
    for m in _OVERFULL_RE.finditer(log_text):
        prefix=log_text[:m.start()]
        files=list(re.finditer(r"\(\.?/?([^()\s]+\.tex)",prefix))
        if files:
            candidate=(latex_dir / files[-1].group(1).replace("/", str(Path('/')))).resolve()
            if candidate.is_file() and candidate not in targets: targets.append(candidate)
    return targets


def diagnostic_sources(log_text: str, latex_dir: Path) -> list[tuple[Path, OverfullBox]]:
    """Return each overfull diagnostic paired with its active TeX source.

    XeLaTeX prints the active ``(./path/file.tex`` line before the warning and
    then reports the offending source line.  Keeping this association lets an
    automatic repair target one display block instead of rewriting every long
    formula in a chapter.
    """
    active: Path | None = None
    result: list[tuple[Path, OverfullBox]] = []
    for line in log_text.splitlines():
        file_match = re.search(r"\(\.?/?([^()\s]+\.tex)", line)
        if file_match:
            candidate = (latex_dir / file_match.group(1).replace("/", "\\")).resolve()
            if candidate.is_file():
                active = candidate
        boxes = detect_overfull_hboxes(line)
        if active is not None:
            result.extend((active, box) for box in boxes)
    return result

def _main(argv: Iterable[str]|None=None) -> int:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("tex",type=Path); ap.add_argument("--output",type=Path); ap.add_argument("--min-chars",type=int,default=110); ap.add_argument("--log",type=Path); ap.add_argument("--line",dest="lines",action="append",type=int,help="only repair a display covering this source line; repeatable")
    args=ap.parse_args(list(argv) if argv is not None else None)
    if args.lines:
        source=args.tex.read_text(encoding="utf-8"); repaired,repairs=repair_latex_source(source,min_chars=args.min_chars,target_lines=args.lines)
        if repairs: (args.output or args.tex).write_text(repaired,encoding="utf-8")
    else:
        repairs=repair_file(args.tex,output=args.output,min_chars=args.min_chars)
    report={"file":str(args.output or args.tex),"repairs":len(repairs)}
    if args.log: report["overfull_boxes"]=[box.__dict__ for box in detect_overfull_hboxes(args.log.read_text(encoding="utf-8",errors="replace"))]
    print(json.dumps(report,ensure_ascii=False,indent=2)); return 0

if __name__=="__main__": raise SystemExit(_main())
