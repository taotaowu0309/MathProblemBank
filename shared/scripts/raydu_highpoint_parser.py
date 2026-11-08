"""Structured parser for 《閱讀高點：旋元佑英文閱讀通》, read from the publisher's PDF.

There is no TeX source of this book, so unlike the other books of the series
it is parsed from the PDF itself.  The PDF is typeset text (not a scan); the
parser works on positioned text lines:

1. :func:`extract_lines` reads every page with PyMuPDF.  Several subset fonts
   (Helvetica, Arial Bold, the question-number font) carry no Unicode map:
   their characters come out as glyph ids, which for letters, digits and ASCII
   punctuation are the code point minus 29 (TrueType standard order).  The
   glyph ids are taken from the text trace and decoded; question numbers are
   circled digits drawn with letter keys (``q w e r`` …).  Lines on one
   baseline (two answer options side by side) are merged.
2. :func:`parse_method` reads the 方法论 (one section per question type);
   :func:`parse_practice_tests` reads the ten chapters of six passages each:
   passage, Exercise (questions), 题解 (文章翻译 and per question the Chinese
   text, 【答案】 and 【解析】).

The book is in Traditional characters; every text is converted to Simplified
(:mod:`shared.scripts.chinese_simplify`) like the rest of the series.  Rich
text uses the runs of :mod:`shared.scripts.raydu_reading_parser` (``b``,
``i``, ``sup``, ``box`` for a sentence-insertion position, ``br``/``line``),
so the question bank and the 阅读方法 page show it with their existing code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.scripts.chinese_simplify import to_simplified
from shared.scripts.raydu_reading_parser import normalize_runs, runs_to_text, text_residues

QUESTION_CODE_PREFIX = "XYHP"
BOOK_TITLE = "旋元佑英文阅读通"
MATERIAL_CODE = "XY-READING-HIGHPOINT"
CHAPTER_COUNT = 10
READINGS_PER_CHAPTER = 6
OPTION_LABELS = ("A", "B", "C", "D")

# Glyph ids without a Unicode map that are not "code point - 29".
_GLYPH_SPECIAL = {135: "•", 171: "…", 179: "—", 180: "“", 181: "”", 182: "‘", 183: "’", 674: "Ⅲ"}
# Circled question numbers 1, 2, 3 … of the question-number font, in its two encodings
# (letter keys in most exercises; decoded glyph ids in the solutions and a few exercises).
_NUMBER_KEYS = ("qwertyuioasdfgh", "KPALNQOEI?M@")

# The book's own names of question types (in the explanations: "这是问细节的题型")
# mapped to the topics of 《旋元佑英文阅读》, so the bank's 题型 filter covers both books.
TOPIC_RULES = (
    ("消去法", "推论", "推论题变型"),
    ("消去法", "", "细节题变型"),
    ("主题", "", "主题型"),
    ("主旨", "", "主题型"),
    ("修辞目的", "", "修辞目的题"),
    ("组织结构", "", "组织结构题"),
    ("安插句子", "", "安插句子题"),
    ("句子改写", "", "句子简化题"),
    ("指射", "", "指涉题"),
    ("语气", "", "语气态度题"),
    ("态度", "", "语气态度题"),
    ("单字", "", "字汇题"),
    ("字汇", "", "字汇题"),
    ("推论", "", "推论题"),
    ("细节", "", "细节题"),
)

# The methodology's sections (by the book's heading) and the 阅读方法 sections of
# 《旋元佑英文阅读》 they belong to, with the bank topics they teach.
METHOD_SECTIONS = {
    "主题型": (("XYRD-M-01-topic",), ("主题型",)),
    "细节型": (("XYRD-M-02-detail",), ("细节题",)),
    "推论型": (("XYRD-M-03-inference",), ("推论题",)),
    "消去法的细节型或推论型": (("XYRD-M-02-detail-variant", "XYRD-M-03-inference-variant"),
                       ("细节题变型", "推论题变型")),
    "单字型": (("XYRD-M-04-vocabulary",), ("字汇题",)),
    "指射型": (("XYRD-M-10-reference",), ("指涉题",)),
    "修辞目的型": (("XYRD-M-06-rhetorical-purpose",), ("修辞目的题",)),
    "组织结构型": (("XYRD-M-07-organization",), ("组织结构题",)),
    "安插句子型": (("XYRD-M-08-insert-sentence",), ("安插句子题",)),
    "语气态度型": (("XYRD-M-05-tone",), ("语气态度题",)),
    "句子改写型": (("XYRD-M-09-sentence-simplification",), ("句子简化题",)),
}
METHOD_INTRO_TARGET = "XYRD-M-high-frequency"

# Typesetting errors of the printed book (a period after "a" became "(A)"; a word
# cut short).  Each correction is reported in the warnings of its passage or question.
TEXT_CORRECTIONS = {"quot(A) The": "quota. The", "Americ(A) ": "America. ", "unleas 字面": "unleash 字面"}


class PdfParseError(ValueError):
    """The PDF does not have the layout the parser knows."""


# ---------------------------------------------------------------------------
# Text extraction


@dataclass
class Span:
    text: str
    font: str
    size: float
    color: int
    x0: float

    @property
    def bold(self) -> bool:
        return "Bold" in self.font

    @property
    def cjk(self) -> bool:
        return self.font.startswith(("AR", "DF"))


@dataclass
class Line:
    page: int  # 1-based PDF page
    x0: float
    top: float
    spans: list[Span] = field(default_factory=list)
    number: int | None = None  # a circled question number at the start of the line

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    def has_font(self, *prefixes: str) -> bool:
        return any(span.font.startswith(prefixes) for span in self.spans)

    def first_font(self) -> str:
        return next((span.font for span in self.spans if span.text.strip()), "")

    def first_size(self) -> float:
        return next((span.size for span in self.spans if span.text.strip()), 0.0)


def _clean(text: str) -> str:
    return "".join(" " if char == "\t" else char for char in text if char >= " " or char == "\t")


def _question_number(text: str) -> int | None:
    key = text.strip()
    for keys in _NUMBER_KEYS:
        if len(key) == 1 and key in keys:
            return keys.index(key) + 1
    return None


def _page_lines(page: Any, page_number: int) -> list[Line]:
    import fitz

    undecoded: dict[tuple[float, float], int] = {}
    for trace in page.get_texttrace():
        for unicode, glyph, origin, _bbox in trace["chars"]:
            if unicode == 0xFFFD:
                undecoded[(round(origin[0], 1), round(origin[1], 1))] = glyph
    raw = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT)
    lines: list[Line] = []
    for block in raw["blocks"]:
        for raw_line in block.get("lines", []):
            spans: list[Span] = []
            number: int | None = None
            for raw_span in raw_line["spans"]:
                chars: list[str] = []
                previous: tuple[float, float] | None = None
                for char in raw_span["chars"]:
                    origin = (round(char["origin"][0], 1), round(char["origin"][1], 1))
                    value = char["c"]
                    if origin in undecoded:
                        glyph = undecoded[origin]
                        value = _GLYPH_SPECIAL.get(glyph, chr(glyph + 29) if 3 <= glyph < 0x62 else "")
                    if origin == previous and chars:
                        # Two glyphs at one place (a control character before a letter):
                        # keep the printable one.
                        if value >= " ":
                            chars[-1] = value
                        continue
                    previous = origin
                    chars.append(value)
                text = _clean("".join(chars))
                if raw_span["font"] == "CombiNumerals":
                    if text.strip():
                        number = _question_number(text)
                        if number is None:
                            raise PdfParseError(f"第 {page_number} 页：无法识别的题号字符 {text!r}")
                    continue
                if raw_span["font"].startswith("DINPro") and (raw_span["size"] <= 8.5 or raw_span["color"] == 0xFFFFFF):
                    continue  # page numbers and the chapter tab in the margin
                if text:
                    spans.append(Span(text, raw_span["font"], round(raw_span["size"], 1),
                                      int(raw_span["color"]), raw_span["bbox"][0]))
            if spans or number is not None:
                x0 = raw_line["bbox"][0]
                lines.append(Line(page_number, x0, raw_line["bbox"][1], spans, number))
    # Merge lines on one baseline (options side by side, a heading number and its title).
    lines.sort(key=lambda line: (round(line.top), line.x0))
    merged: list[Line] = []
    for line in lines:
        last = merged[-1] if merged else None
        if last is not None and abs(last.top - line.top) <= 2.5:
            spans = sorted([*last.spans, *line.spans], key=lambda span: span.x0)
            last.spans = spans
            last.x0 = min(last.x0, line.x0)
            last.number = last.number if last.number is not None else line.number
        else:
            merged.append(line)
    for line in merged:
        if line.spans:
            body = max((span.size for span in line.spans), default=0.0)
            for span in line.spans:
                if span.size < body * 0.7 and span.text.strip() in {"th", "st", "nd", "rd"}:
                    span.font += "|sup"
    return [line for line in merged if line.text.strip() or line.number is not None]


def extract_lines(pdf_path: Path | str) -> tuple[list[Line], list[list[Any]]]:
    """Every text line of the book in reading order, and the PDF outline."""

    import fitz

    with fitz.open(str(pdf_path)) as document:
        toc = document.get_toc()
        lines: list[Line] = []
        for index, page in enumerate(document):
            lines.extend(_page_lines(page, index + 1))
    return lines, toc


def _margin(page: int) -> float:
    """The text block's left edge: the book alternates left and right pages."""

    return 56.0 if page % 2 == 0 else 70.0


def _indented(line: Line, by: float = 12.0) -> bool:
    return line.x0 >= _margin(line.page) + by


# ---------------------------------------------------------------------------
# Runs


def _span_run(span: Span, *, bold: bool | None = None, italic: bool = False) -> dict[str, Any]:
    text = to_simplified(span.text)
    run: dict[str, Any] = {"t": text}
    if span.font.endswith("|sup"):
        run["sup"] = True
    if span.bold if bold is None else bold:
        run["b"] = True
    if italic:
        run["i"] = True
    return run


def _join(runs: list[dict[str, Any]], more: list[dict[str, Any]]) -> None:
    """Append a line's runs to a paragraph: a space between English words,
    nothing after a hyphen or between Chinese characters."""

    previous = runs_to_text(runs)
    following = runs_to_text(more)
    if previous and following and not previous[-1].isspace() and not following[0].isspace():
        cjk = "\u3000" <= previous[-1] <= "\uffef" or "\u3000" <= following[0] <= "\uffef"
        if not cjk and previous[-1] not in "-—‘“(" and following[0] not in ",.;:?!)’”—":
            # The space joins the last run, so a bold sentence over two lines stays one run.
            last = next((index for index in range(len(runs) - 1, -1, -1) if "t" in runs[index]), None)
            if last is not None and last == len(runs) - 1:
                runs[last] = {**runs[last], "t": runs[last]["t"] + " "}
            else:
                runs.append({"t": " "})
    runs.extend(more)


def _line_runs(line: Line, *, bold: bool | None = None, italic: bool = False,
               boxes: bool = False) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for span in line.spans:
        letter = span.text.strip()
        if boxes and span.bold and letter in {"W", "X", "Y", "Z"}:
            runs.append({"t": " "})
            runs.append({"box": letter})
            runs.append({"t": " "})
            continue
        runs.append(_span_run(span, bold=bold, italic=italic))
    return runs


def _finish(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse spaces, trim the ends and correct the book's typesetting errors."""

    cleaned: list[dict[str, Any]] = []
    for run in normalize_runs(runs):
        if "t" in run:
            text = re.sub(r"[ \u00a0]+", " ", run["t"])
            for wrong, right in TEXT_CORRECTIONS.items():
                text = text.replace(wrong, right)
            previous = cleaned[-1] if cleaned else None
            if text.startswith(" ") and (previous is None or previous.get("line")
                                         or str(previous.get("t", "")).endswith(" ")):
                text = text[1:]
            run = {**run, "t": text}
        cleaned.append(run)
    while cleaned and "t" in cleaned[0] and not cleaned[0]["t"].strip():
        cleaned.pop(0)
    while cleaned and "t" in cleaned[-1] and not cleaned[-1]["t"].strip():
        cleaned.pop()
    if cleaned and "t" in cleaned[0]:
        cleaned[0] = {**cleaned[0], "t": cleaned[0]["t"].lstrip()}
    if cleaned and "t" in cleaned[-1]:
        cleaned[-1] = {**cleaned[-1], "t": cleaned[-1]["t"].rstrip()}
    runs = _space_mixed(normalize_runs(cleaned))
    # Spaces around a box are kept single; no space before punctuation.
    for index in range(1, len(runs)):
        if "t" in runs[index] and "t" in runs[index - 1] and runs[index - 1]["t"].endswith(" ") \
                and runs[index]["t"][:1] in ",.;:?!":
            runs[index - 1] = {**runs[index - 1], "t": runs[index - 1]["t"].rstrip()}
    return [run for run in runs if run.get("t", "x") != ""]


_HAN = "\u4e00-\u9fff"
_MIXED = re.compile(rf"(?<=[{_HAN}])(?=[A-Za-z0-9(])|(?<=[A-Za-z0-9)%])(?=[{_HAN}])")


def _space_mixed(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A space between a Chinese character and English or a number, as everywhere in
    《英文阅读》 (the PDF has it in most places but not all: "是grass-eating")."""

    result: list[dict[str, Any]] = []
    previous = ""
    for run in runs:
        if "t" not in run:
            result.append(run)
            previous = ""
            continue
        text = _MIXED.sub(" ", run["t"])
        if previous and text and _MIXED.match(previous[-1] + text[0], 1) is not None \
                and not previous[-1].isspace() and not text[0].isspace():
            text = " " + text
        result.append({**run, "t": text})
        previous = text
    return result


def _paragraphs(lines: list[Line], *, boxes: bool = False, indent: float = 12.0) -> list[dict[str, Any]]:
    """Display blocks (``para``) of consecutive lines; a paragraph starts at an indented line."""

    blocks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] | None = None
    for line in lines:
        runs = _line_runs(line, boxes=boxes)
        if current is None or _indented(line, indent):
            current = []
            blocks.append({"kind": "para", "runs": current})
        _join(current, runs)
    for block in blocks:
        block["runs"] = _finish(block["runs"])
    return [block for block in blocks if block["runs"]]


# ---------------------------------------------------------------------------
# The book's outline


def _chapter_pages(toc: list[list[Any]], end_page: int) -> tuple[int, int, list[tuple[int, int, int]]]:
    """The methodology's page range and every chapter's (number, first page, last page)."""

    pages = {to_simplified(str(title).strip()): int(page) for _level, title, page, *_rest in toc}
    method = pages.get("方法论")
    tests = pages.get("阅读测验篇")
    chapters = sorted(
        (int(match.group(1)), int(page))
        for _level, title, page, *_rest in toc
        if (match := re.fullmatch(r"第\s*(\d+)\s*章", str(title).strip()))
    )
    if method is None or tests is None or [number for number, _page in chapters] != list(range(1, CHAPTER_COUNT + 1)):
        raise PdfParseError("PDF 书签与《旋元佑英文阅读通》的结构不符（需要“方法论”“阅读测验篇”和第 1–10 章）。")
    ranges = []
    for index, (number, first) in enumerate(chapters):
        last = chapters[index + 1][1] - 1 if index + 1 < len(chapters) else end_page
        ranges.append((number, first, last))
    return method, tests - 1, ranges


def parse_themes(lines: list[Line]) -> dict[tuple[int, int], str]:
    """文章主题分类概览: the subject of every passage, keyed by (chapter, article)."""

    page = next((line.page for line in lines if line.text.strip() == "文章主題分類概覽"), None)
    if page is None:
        return {}
    rows: dict[str, list[tuple[float, int, str]]] = {"left": [], "right": []}
    for line in lines:
        if line.page != page:
            continue
        cells = [span for span in line.spans if span.text.strip()]
        # A row: article number (DIN font) and its subject, in the left or the right half.
        for position, span in enumerate(cells):
            if span.font.startswith("DINPro") and span.text.strip().isdigit() and position + 1 < len(cells):
                subject = ""
                for following in cells[position + 1:]:
                    if following.font.startswith("DINPro"):
                        break
                    subject += following.text
                side = "left" if span.x0 < 250 else "right"
                rows[side].append((line.top, int(span.text), to_simplified(re.sub(r"\s+", "", subject))))
    themes: dict[tuple[int, int], str] = {}
    for side, first_chapter in (("left", 1), ("right", 6)):
        ordered = sorted(rows[side])
        for index, (_top, article, subject) in enumerate(ordered):
            themes[(first_chapter + index // READINGS_PER_CHAPTER, article)] = subject
    return themes


# ---------------------------------------------------------------------------
# The methodology


def _is_heading(line: Line) -> bool:
    return any(span.font.startswith("DFLiHei") and span.size >= 12.5 for span in line.spans)


def parse_method(lines: list[Line], first_page: int, last_page: int) -> list[dict[str, Any]]:
    """The question-type sections of 方法论, each a list of display blocks."""

    sections: list[dict[str, Any]] = []
    current = {"title": "", "number": "", "source_page": first_page + 1, "lines": []}
    sections.append(current)
    for line in lines:
        if not first_page < line.page <= last_page:
            continue
        if _is_heading(line):
            number = "".join(span.text for span in line.spans if span.font.startswith("FoundryJournal")).strip()
            title = "".join(span.text for span in line.spans if span.font.startswith("DFLiHei")).strip()
            current = {"title": to_simplified(title), "number": number, "source_page": line.page, "lines": []}
            sections.append(current)
            continue
        current["lines"].append(line)
    result = []
    for index, section in enumerate(sections):
        blocks = _method_blocks(section["lines"])
        if not blocks:
            continue
        title = section["title"] or "方法论导言"
        key = f"{QUESTION_CODE_PREFIX}-M-{index:02d}"
        targets, topics = METHOD_SECTIONS.get(title, ((METHOD_INTRO_TARGET,), ()) if not section["title"] else ((), ()))
        result.append({
            "key": key, "title": title, "number": section["number"], "source_page": section["source_page"],
            "blocks": blocks, "targets": list(targets), "topics": list(topics),
        })
    return result


def _method_role(line: Line) -> str:
    text = line.text.strip()
    if text.startswith("•"):
        return "bullet"
    if re.fullmatch(r"第\s*\d+\s*段：", text):
        return "label"
    if line.has_font("ARMing"):
        return "body"
    font = line.first_font()
    if font.startswith(("FranklinGothic", "FoundryJournal")):
        return "excerpt"
    if font.startswith(("Helvetica", "Arial-Bold")) and not line.has_font("AR", "DF"):
        return "wording"
    if font.startswith(("ARHei", "ArialMT")):
        return "translation"
    if font.startswith("TimesNewRoman"):
        return "words"
    return "body"


def _method_blocks(lines: list[Line]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    last_role = ""
    list_x0 = 0.0
    for line in lines:
        role = _method_role(line)
        block = blocks[-1] if blocks else None
        if role == "bullet":
            item = _line_runs(line)
            item[0] = {**item[0], "t": item[0]["t"].lstrip("•").lstrip()}
            if block is not None and block["kind"] == "list" and last_role in {"bullet", "bullet_more"}:
                block["items"].append(item)
            else:
                blocks.append({"kind": "list", "items": [item], "ordered": False})
            list_x0 = line.x0
            last_role = "bullet"
            continue
        if last_role in {"bullet", "bullet_more"} and role != "body" and line.x0 > list_x0 + 3 \
                and block is not None and block["kind"] == "list":
            more = _line_runs(line)
            if role == "words":
                block["items"][-1].append({"line": True})
                block["items"][-1].extend(more)
            else:
                _join(block["items"][-1], more)
            last_role = "bullet_more"
            continue
        if role == "label":
            blocks.append({"kind": "label", "text": to_simplified(re.sub(r"\s+", " ", line.text.strip()))})
        elif role == "body" or role == "words":
            if block is not None and block["kind"] == "para" and block.get("role") == "body" \
                    and last_role == "body" and not _indented(line):
                _join(block["runs"], _line_runs(line))
            else:
                blocks.append({"kind": "para", "role": "body", "runs": _line_runs(line)})
            role = "body"
        elif role in {"excerpt", "wording"}:
            runs = _line_runs(line, bold=role == "wording" or None, italic=role == "excerpt")
            if block is not None and block.get("role") == role and last_role == role:
                _join(block["runs"], runs)
            else:
                blocks.append({"kind": "para", "role": role, "runs": runs})
        elif role == "translation":
            runs = _line_runs(line, bold=False)
            if block is not None and block["kind"] == "para" and last_role in {"excerpt", "wording"}:
                block["runs"].append({"line": True})
                block["runs"].extend(runs)
                role = "attached"
            elif block is not None and block["kind"] == "para" and last_role in {"attached", "translation"}:
                _join(block["runs"], runs)
                role = "attached" if last_role == "attached" else "translation"
            else:
                blocks.append({"kind": "para", "role": "translation", "runs": runs})
        last_role = role
    for block in blocks:
        if block["kind"] == "para":
            block["runs"] = _finish(block["runs"])
            block.pop("role", None)
        elif block["kind"] == "list":
            block["items"] = [_finish(item) for item in block["items"]]
    return [block for block in blocks if block.get("runs") or block.get("items") or block.get("text")]


# ---------------------------------------------------------------------------
# The practice tests


@dataclass
class _Question:
    number: int
    page: int
    stem: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    zh: list[dict[str, Any]] = field(default_factory=list)
    zh_options: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    explanation: list[dict[str, Any]] = field(default_factory=list)
    solved: bool = False


_OPTION_SPLIT = re.compile(r"\(([A-D])\)\s*")


def _split_options(line: Line, *, bold: bool | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
    """``(A) raise  (B) limit`` → [("A", runs), ("B", runs)] (one line may hold two options)."""

    pieces: list[tuple[str, list[dict[str, Any]]]] = []
    for span in line.spans:
        text = span.text
        position = 0
        for match in _OPTION_SPLIT.finditer(text):
            before = text[position:match.start()]
            if before and pieces:
                pieces[-1][1].append(_span_run(Span(before, span.font, span.size, span.color, span.x0), bold=bold))
            pieces.append((match.group(1), []))
            position = match.end()
        rest = text[position:]
        if rest and pieces:
            pieces[-1][1].append(_span_run(Span(rest, span.font, span.size, span.color, span.x0), bold=bold))
    return pieces


def _starts_with_option(line: Line) -> bool:
    return bool(re.match(r"\s*\([A-D]\)", line.text))


def _marker(line: Line) -> str:
    text = line.text.strip()
    font = line.first_font()
    size = line.first_size()
    if font.startswith("FoundryJournal") and re.fullmatch(r"Reading \d+", text):
        return "reading"
    if font.startswith("FoundryJournal") and text == "Exercise":
        return "exercise"
    if font.startswith("FoundryJournal") and text == "NOTES":
        return "skip"
    if font.startswith("ARStdSong") and text == "題解":
        return "solutions"
    if text == "文章翻譯" and size > 9.3:
        return "translation"
    if text == "練習題翻譯與詳解":
        return "explanations"
    if font.startswith("ARStdSong"):
        return "skip"  # chapter title pages
    return ""


def parse_practice_tests(lines: list[Line], chapters: list[tuple[int, int, int]],
                         themes: dict[tuple[int, int], str]) -> dict[str, Any]:
    """The 60 passages and their questions, answers, translations and explanations."""

    groups: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for chapter, first, last in chapters:
        units: list[dict[str, Any]] = []
        unit: dict[str, Any] | None = None
        state = ""
        for line in lines:
            if not first <= line.page <= last:
                continue
            marker = _marker(line)
            if marker == "skip":
                continue
            if marker == "reading":
                unit = {"number": int(line.text.split()[-1]), "page": line.page, "passage": [], "questions": [],
                        "translation": [], "title": ""}
                units.append(unit)
                state = "passage"
                continue
            if unit is None:
                continue
            if marker in {"exercise", "solutions", "translation", "explanations"}:
                state = marker
                continue
            try:
                _read_line(unit, state, line)
            except PdfParseError as error:
                issues.append(f"Chapter {chapter} · Reading {unit['number']}：{error}")
        numbers = [item["number"] for item in units]
        if numbers != list(range(1, READINGS_PER_CHAPTER + 1)):
            issues.append(f"Chapter {chapter}：文章编号为 {numbers}，应为 1–{READINGS_PER_CHAPTER}")
        for item in units:
            group, built, problems = _build_unit(chapter, item, themes)
            issues.extend(problems)
            warnings.extend(f"{group['group_code']}：{text}" for text in group["warnings"])
            groups.append(group)
            bucket = counts.setdefault(f"{chapter:02d}", {"groups": 0, "questions": 0})
            bucket["groups"] += 1
            for question in built:
                warnings.extend(f"{question['question_code']}：{text}" for text in question["warnings"])
                issues.extend(f"{question['question_code']}：{text}" for text in validate_question(question))
                questions.append(question)
                bucket["questions"] += 1
    codes = [item["question_code"] for item in questions]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        issues.append("题号重复：" + "、".join(duplicates))
    return {"groups": groups, "questions": questions, "issues": issues, "warnings": warnings, "counts": counts}


def _read_line(unit: dict[str, Any], state: str, line: Line) -> None:
    questions: list[_Question] = unit["questions"]
    if state == "passage":
        if line.text.strip():
            unit["passage"].append(line)
        return
    if state == "translation":
        unit["translation"].append(line)
        return
    if state == "exercise":
        if line.number is not None:
            questions.append(_Question(line.number, line.page, _line_runs(line)))
            return
        if not questions:
            if line.text.strip():
                raise PdfParseError(f"Exercise 第一题之前有无法归属的文字：{line.text.strip()[:40]}")
            return
        question = questions[-1]
        if not line.text.strip():
            return
        if _starts_with_option(line):
            for label, runs in _split_options(line):
                question.options.append({"label": label, "runs": runs})
        elif question.options:
            _join(question.options[-1]["runs"], _line_runs(line))
        elif all(span.bold or not span.text.strip() for span in line.spans):
            # The bold sentence quoted by the question (to insert, or to restate).
            if not question.stem or question.stem[-1].get("b") is None:
                question.stem.append({"line": True})
            _join(question.stem, _line_runs(line))
        else:
            _join(question.stem, _line_runs(line))
        return
    if state == "explanations":
        if line.number is not None:
            question = next((item for item in questions if item.number == line.number and not item.solved), None)
            if question is None:
                raise PdfParseError(f"题解第 {line.number} 题在 Exercise 中没有对应的题目")
            question.solved = True
            question.zh = _line_runs(line, bold=False)
            unit["solving"] = question
            unit["solving_part"] = "zh"
            return
        question = unit.get("solving")
        text = line.text.strip()
        if question is None:
            if text:
                raise PdfParseError(f"题解第一题之前有无法归属的文字：{text[:40]}")
            return
        if not text:
            return
        if text.startswith("【答案】"):
            question.answer = re.sub(r"\s+", "", text[len("【答案】"):])
            unit["solving_part"] = "answer"
            return
        if text.startswith("【解析】"):
            runs = _line_runs(line, bold=False)
            runs[0] = {**runs[0], "t": runs[0]["t"].replace("【解析】", "", 1)}
            question.explanation = runs
            unit["solving_part"] = "explanation"
            return
        part = unit.get("solving_part")
        if part == "explanation":
            _join(question.explanation, _line_runs(line, bold=False))
        elif _starts_with_option(line) and part in {"zh", "zh_options"}:
            for label, runs in _split_options(line, bold=False):
                question.zh_options.append({"label": label, "runs": runs})
            unit["solving_part"] = "zh_options"
        elif part == "zh_options":
            _join(question.zh_options[-1]["runs"], _line_runs(line, bold=False))
        elif part == "zh":
            if any(span.bold for span in line.spans if span.text.strip()):
                # The quoted bold sentence, on a line of its own (it may run over two lines).
                if not (question.zh and question.zh[-1].get("b")):
                    question.zh.append({"line": True})
                _join(question.zh, _line_runs(line))
            else:
                _join(question.zh, _line_runs(line, bold=False))
        else:
            raise PdfParseError(f"第 {question.number} 题的题解中有无法归属的文字：{text[:40]}")


def _topic(explanation: list[dict[str, Any]], stem: str) -> str:
    text = runs_to_text(explanation)
    # "这是推论题、并采用消去法的题型": up to 题型 when the clause has it, else to 题.
    head = re.search(r"这也?是[^。，]{0,30}?题型", text) or re.search(r"这也?是[^。，]{0,12}?题", text)
    named = head.group(0) if head else ""
    for first, second, topic in TOPIC_RULES:
        if first in named and second in named:
            return topic
    lowered = stem.lower()
    # (The book misprints "closest" as "closet" twice.)
    if re.search(r"closes?t in meaning|best be replaced|could best replace|is closest to|most (?:nearly|closely) means",
                 lowered):
        return "字汇题"
    if re.search(r"refers? to", lowered):
        return "指涉题"
    return ""


def _build_unit(chapter: int, unit: dict[str, Any], themes: dict[tuple[int, int], str]
                ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    number = unit["number"]
    group_code = f"{QUESTION_CODE_PREFIX}-{chapter:02d}-{number}"
    theme = themes.get((chapter, number), "")
    subject = theme.split("（")[0] if theme else ""
    problems: list[str] = []
    # The words a question asks about are printed bold: the book's key words, as in 《英文阅读》.
    blocks = [{**block, "runs": [{**{k: v for k, v in run.items() if k != "b"}, "em": True} if run.get("b") else run
                                 for run in block["runs"]]}
              for block in _paragraphs(unit["passage"], boxes=True)]
    translation = _paragraphs(unit["translation"])
    # The translation marks the same insertion positions: boxes there too.
    letters = {run["box"] for block in blocks for run in block["runs"] if "box" in run}
    if letters:
        translation = [{**block, "runs": _box_letters(block["runs"], letters)} for block in translation]
    group_warnings = _corrections(" ".join(line.text for line in unit["passage"]), "文章")
    if not blocks:
        problems.append(f"{group_code}：没有读到文章")
    if not translation:
        problems.append(f"{group_code}：没有读到文章翻译")
    residues = text_residues([blocks, translation])
    if residues:
        problems.append(f"{group_code}：文章残留无法识别的字符：" + " ".join(residues))
    title = f"第 {chapter} 章 · Reading {number}" + (f" · {theme}" if theme else "")
    group = {
        "group_code": group_code,
        "chapter_number": chapter,
        "article_number": number,
        "theme": theme,
        "subject": subject,
        "title": title,
        "blocks": blocks,
        "translation": translation,
        "source_page": unit["page"],
        "warnings": group_warnings,
    }
    built: list[dict[str, Any]] = []
    numbers = [question.number for question in unit["questions"]]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"{group_code}：题号不连续 {numbers}")
    for question in unit["questions"]:
        item_warnings: list[str] = []
        stem = _finish(question.stem)
        options = _relabel(question.options, "选项", item_warnings)
        zh_stem = _finish(question.zh)
        zh_options = _relabel(question.zh_options, "选项翻译", item_warnings)
        zh = list(zh_stem)
        # One option per line (the book pairs short ones, but rich text collapses the gap).
        for option in zh_options:
            zh.append({"line": True})
            zh.append({"t": f"({option['label']}) "})
            zh.extend(option["runs"])
        item_warnings.extend(_corrections(runs_to_text(question.explanation), "解析"))
        explanation = _finish(question.explanation)
        stem_text = runs_to_text(stem)
        topic = _topic(explanation, stem_text)
        if not topic:
            item_warnings.append("原书解析没有标出题型")
        if not question.solved:
            problems.append(f"{group_code}-{question.number:02d}：题解中没有这道题")
        built.append({
            "question_code": f"{group_code}-{question.number:02d}",
            "group_code": group_code,
            "chapter_number": chapter,
            "article_number": number,
            "number": question.number,
            "number_label": str(question.number),
            "theme": theme,
            "subject": subject,
            "stem": stem,
            "options": options,
            "option_layout": "one_column",
            "answer": question.answer,
            "zh": normalize_runs(zh),
            "zh_options": zh_options,
            "explanation": explanation,
            "topic": topic,
            "source_page": question.page,
            "warnings": item_warnings,
        })
    return group, built, problems


def _relabel(options: list[dict[str, Any]], what: str, warnings: list[str]) -> list[dict[str, Any]]:
    """Four options in order, labelled A–D.  The book once prints (A) (C) (C) (D);
    a misprinted label of four options is corrected by position and reported."""

    labels = [option["label"] for option in options]
    if len(options) == len(OPTION_LABELS) and labels != list(OPTION_LABELS):
        warnings.append(f"原书{what}标号印成 {'、'.join(labels)}，已按顺序更正为 A–D")
        labels = list(OPTION_LABELS)
    return [{"label": label, "runs": _finish(option["runs"])} for label, option in zip(labels, options)]


def _corrections(raw: str, where: str) -> list[str]:
    return [f"原书{where}印作 “{wrong.strip()}”，已更正为 “{right.strip()}”"
            for wrong, right in TEXT_CORRECTIONS.items() if wrong in raw]


def _box_letters(runs: list[dict[str, Any]], letters: set[str]) -> list[dict[str, Any]]:
    """Standalone W/X/Y/Z of a translation as insertion boxes."""

    pattern = re.compile(r"(?<![A-Za-z])([" + "".join(sorted(letters)) + r"])(?![A-Za-z])")
    result: list[dict[str, Any]] = []
    for run in runs:
        if "t" not in run:
            result.append(run)
            continue
        position = 0
        for match in pattern.finditer(run["t"]):
            if match.start() > position:
                result.append({**run, "t": run["t"][position:match.start()]})
            result.append({"box": match.group(1)})
            position = match.end()
        if position < len(run["t"]):
            result.append({**run, "t": run["t"][position:]})
    return result


def validate_question(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    labels = [option["label"] for option in item["options"]]
    if labels != list(OPTION_LABELS):
        errors.append(f"选项标号不是 A–D：{labels}")
    if any(not option["runs"] for option in item["options"]):
        errors.append("有空选项")
    if item["answer"] not in OPTION_LABELS:
        errors.append(f"答案 {item['answer']!r} 不是 A–D")
    if not item["stem"]:
        errors.append("题干为空")
    if [option["label"] for option in item["zh_options"]] != list(OPTION_LABELS):
        errors.append("题解中的选项翻译不全")
    if not item["explanation"]:
        errors.append("没有解析")
    residues = text_residues([item["stem"], item["zh"], item["explanation"], item["options"]])
    if residues:
        errors.append("残留无法识别的字符：" + " ".join(residues))
    return errors


# ---------------------------------------------------------------------------
# The whole book


def parse_book(pdf_path: Path | str) -> dict[str, Any]:
    """Methodology sections, passages and questions of the book, with every problem found."""

    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"未找到《旋元佑英文阅读通》PDF：{path}")
    lines, toc = extract_lines(path)
    # The book ends before its copyright page (国家图书馆出版品预行编目资料).
    end_page = next((line.page - 1 for line in lines if "出版品預行編目" in line.text),
                    max((line.page for line in lines), default=0))
    method_first, method_last, chapters = _chapter_pages(toc, end_page)
    themes = parse_themes(lines)
    method = parse_method(lines, method_first, method_last)
    practice = parse_practice_tests(lines, chapters, themes)
    issues = list(practice["issues"])
    if len(themes) != CHAPTER_COUNT * READINGS_PER_CHAPTER:
        issues.append(f"文章主题分类概览读到 {len(themes)} 篇，应为 {CHAPTER_COUNT * READINGS_PER_CHAPTER} 篇")
    mapped = {section["title"] for section in method if section["targets"]}
    missing = sorted(set(METHOD_SECTIONS) - mapped)
    if missing:
        issues.append("方法论缺少题型小节：" + "、".join(missing))
    unknown = [section["title"] for section in method if not section["targets"]]
    if unknown:
        issues.append("方法论有无法对应到阅读方法的小节：" + "、".join(unknown))
    return {**practice, "issues": issues, "method": method, "themes": {f"{c}-{a}": t for (c, a), t in themes.items()}}


__all__ = [
    "BOOK_TITLE", "MATERIAL_CODE", "METHOD_SECTIONS", "QUESTION_CODE_PREFIX", "PdfParseError",
    "extract_lines", "parse_book", "parse_method", "parse_practice_tests", "parse_themes", "validate_question",
]
