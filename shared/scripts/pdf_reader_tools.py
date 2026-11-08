"""PDF reader helpers that do not need Qt: in-document search, references, page labels.

The Qt reader (``QtPdfPreviewWindow`` in ``problem_bank_center_qt.py``) uses
these for Ctrl+F search, the hover preview of "Theorem 3.2" / "(2.14)" /
"[12]" references and the printed-page box.  Everything works on PyMuPDF word
tuples ``(x0, y0, x1, y1, text, block, line, word)`` so it can be tested
without a window and run in a background thread.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

Rect = tuple[float, float, float, float]

# -- words -------------------------------------------------------------------


def _fitz() -> Any:
    try:
        import pymupdf as module  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - older PyMuPDF
        import fitz as module  # type: ignore[import-not-found,no-redef]
    return module


def page_words(page: Any) -> list[tuple]:
    """Words of a PyMuPDF page in reading order (block, line, word)."""

    words = [tuple(word) for word in (page.get_text("words") or []) if len(word) >= 8]
    words.sort(key=lambda word: (int(word[5]), int(word[6]), int(word[7])))
    return words


def _normal(text: str) -> str:
    # Ligatures (ﬁ, ﬀ) and full-width forms read as plain letters.
    return unicodedata.normalize("NFKC", text)


def _lines(words: Sequence[tuple]) -> list[list[tuple]]:
    lines: list[list[tuple]] = []
    key: tuple[int, int] | None = None
    for word in words:
        current = (int(word[5]), int(word[6]))
        if current != key:
            lines.append([])
            key = current
        lines[-1].append(word)
    return lines


# -- search -------------------------------------------------------------------


@dataclass
class SearchHit:
    page: int
    rects: list[Rect]
    context: str
    start: int = 0

    @property
    def y0(self) -> float:
        return min(rect[1] for rect in self.rects) if self.rects else 0.0

    def as_dict(self) -> dict[str, Any]:
        x0 = min(rect[0] for rect in self.rects)
        x1 = max(rect[2] for rect in self.rects)
        y1 = max(rect[3] for rect in self.rects)
        return {"page": self.page, "x0": x0, "y0": self.y0, "x1": x1, "y1": y1, "rects": list(self.rects), "context": self.context}


def page_search_text(words: Sequence[tuple]) -> tuple[str, list[tuple[int, int, int]]]:
    """Join a page's words into one searchable string.

    Words are separated by one space; a word hyphenated at a line end is joined
    to its continuation so "com-" + "pact" matches "compact".  Returns the text
    and, per word, ``(start, end, word_index)`` offsets into it.
    """

    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    position = 0
    for index, word in enumerate(words):
        text = _normal(str(word[4]))
        last_in_line = index + 1 == len(words) or (int(words[index + 1][5]), int(words[index + 1][6])) != (int(word[5]), int(word[6]))
        hyphenated = last_in_line and index + 1 < len(words) and len(text) > 1 and text.endswith("-") and text[-2].isalpha()
        if hyphenated:
            text = text[:-1]
        spans.append((position, position + len(text), index))
        parts.append(text)
        position += len(text)
        if not hyphenated:
            parts.append(" ")
            position += 1
    return "".join(parts), spans


def search_pattern(query: str, *, case_sensitive: bool = False, whole_word: bool = False) -> re.Pattern[str] | None:
    pieces = [re.escape(_normal(piece)) for piece in query.split()]
    if not pieces:
        return None
    body = r"\s*".join(pieces) if len(pieces) == 1 else r"\s+".join(pieces)
    if whole_word:
        body = rf"(?<![^\W_]){body}(?![^\W_])"
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def search_words(page_index: int, words: Sequence[tuple], pattern: re.Pattern[str], *, context_chars: int = 46) -> list[SearchHit]:
    text, spans = page_search_text(words)
    hits: list[SearchHit] = []
    for match in pattern.finditer(text):
        start, end = match.span()
        if end <= start:
            continue
        rects: list[Rect] = []
        for span_start, span_end, word_index in spans:
            if span_end <= start or span_start >= end:
                continue
            word = words[word_index]
            length = max(1, span_end - span_start)
            first = (max(start, span_start) - span_start) / length
            last = (min(end, span_end) - span_start) / length
            x0, y0, x1, y1 = (float(value) for value in word[:4])
            rect = (x0 + (x1 - x0) * first, y0, x0 + (x1 - x0) * last, y1)
            if rects and abs(rects[-1][1] - y0) < 1.0 and abs(rects[-1][3] - y1) < 1.0 and rect[0] - rects[-1][2] < 8:
                previous = rects[-1]
                rects[-1] = (previous[0], previous[1], max(previous[2], rect[2]), previous[3])
            else:
                rects.append(rect)
        if not rects:
            continue
        before = text[max(0, start - context_chars):start]
        after = text[end:end + context_chars]
        context = ("…" if start > context_chars else "") + before + "【" + text[start:end] + "】" + after + ("…" if end + context_chars < len(text) else "")
        hits.append(SearchHit(page_index, rects, " ".join(context.split()), start))
    return hits


def search_document(
    document: Any,
    query: str,
    *,
    case_sensitive: bool = False,
    whole_word: bool = False,
    pages: Iterable[int] | None = None,
) -> list[SearchHit]:
    pattern = search_pattern(query, case_sensitive=case_sensitive, whole_word=whole_word)
    if pattern is None:
        return []
    hits: list[SearchHit] = []
    for page_index in pages if pages is not None else range(int(document.page_count)):
        hits.extend(search_words(page_index, page_words(document.load_page(page_index)), pattern))
    return hits


# -- references: "Theorem 3.2", "(2.14)", "Section 4.1" -----------------------

REFERENCE_KINDS = {
    "theorem": "theorem", "thm": "theorem",
    "lemma": "lemma", "lem": "lemma",
    "proposition": "proposition", "prop": "proposition",
    "corollary": "corollary", "cor": "corollary",
    "definition": "definition", "defn": "definition", "def": "definition",
    "example": "example", "ex": "example",
    "remark": "remark", "rem": "remark",
    "exercise": "exercise", "problem": "problem",
    "claim": "claim", "conjecture": "conjecture", "fact": "fact",
    "notation": "notation", "observation": "observation",
    "algorithm": "algorithm", "assumption": "assumption", "axiom": "axiom",
    "figure": "figure", "fig": "figure", "table": "table",
    "section": "section", "sec": "section", "§": "section",
    "chapter": "chapter", "ch": "chapter", "appendix": "appendix",
    "定理": "theorem", "引理": "lemma", "命题": "proposition", "推论": "corollary",
    "定义": "definition", "例": "example", "注": "remark", "图": "figure", "表": "table",
}
KIND_TITLES = {
    "theorem": "Theorem", "lemma": "Lemma", "proposition": "Proposition", "corollary": "Corollary",
    "definition": "Definition", "example": "Example", "remark": "Remark", "exercise": "Exercise",
    "problem": "Problem", "claim": "Claim", "conjecture": "Conjecture", "fact": "Fact",
    "notation": "Notation", "observation": "Observation", "algorithm": "Algorithm",
    "assumption": "Assumption", "axiom": "Axiom", "figure": "Figure", "table": "Table",
    "section": "Section", "chapter": "Chapter", "appendix": "Appendix", "equation": "Equation",
}
_NUMBER_RE = re.compile(r"^(?:[A-Z]\.)?\d+(?:\.\d+)*[a-z]?$|^[A-Z](?:\.\d+)+$")
_EQUATION_RE = re.compile(r"^\((?:[A-Z]\.)?\d+(?:\.\d+)*[a-z]?\)$")
_SECTION_HEADING_RE = re.compile(r"^\d+(?:\.\d+)+$")


def _kind(token: str) -> str:
    cleaned = _normal(token).strip().rstrip(".:").lower()
    if cleaned.startswith("§") and len(cleaned) > 1:
        return ""
    return REFERENCE_KINDS.get(cleaned, "")


_GLUED_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)+)[.:]?(?:[(（]|[A-Z])")
_GLUED_HEADING_RE = re.compile(r"^([A-Za-z]+)\.?(\d+(?:\.\d+)+)(?=$|[.:(（A-Z])")


def _number(token: str) -> str:
    cleaned = _normal(token).strip().rstrip(".,:;)")
    if _NUMBER_RE.match(cleaned):
        return cleaned
    # OCR glues a heading's name to its number: "4.5.5(Koszul".
    glued = _GLUED_NUMBER_RE.match(_normal(token).strip())
    return glued.group(1) if glued else ""


def _equation(token: str) -> str:
    cleaned = _normal(token).strip().rstrip(".,:;")
    return cleaned[1:-1] if _EQUATION_RE.match(cleaned) else ""


def _line_text(line: Sequence[tuple], limit: int = 110) -> str:
    text = " ".join(_normal(str(word[4])) for word in line)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Groups of the 定理 sidebar list.
STATEMENT_GROUPS = {
    "theorem": "定理类", "lemma": "定理类", "proposition": "定理类", "corollary": "定理类", "claim": "定理类",
    "conjecture": "定理类", "fact": "定理类", "observation": "定理类", "axiom": "定理类",
    "definition": "定义", "notation": "定义", "assumption": "定义",
    "example": "例子", "remark": "注记",
    "exercise": "习题", "problem": "习题",
    "figure": "图表", "table": "图表", "algorithm": "图表",
    "equation": "公式",
}


@dataclass(frozen=True)
class ReferenceTarget:
    kind: str
    number: str
    page: int
    y: float
    x: float = 0.0
    text: str = ""

    @property
    def label(self) -> str:
        if self.kind == "equation":
            return f"({self.number})"
        return f"{KIND_TITLES.get(self.kind, self.kind.title())} {self.number}"


def reference_definitions(page_index: int, words: Sequence[tuple], page_width: float) -> list[ReferenceTarget]:
    """Where a page states numbered items: theorem headings, equation tags, section headings.

    A heading counts only at the start of a text block ("Theorem 3.2." opening a
    paragraph), so "Theorem 3.2 shows ..." wrapped onto a new line is not taken
    for its statement.  An equation tag "(2.14)" counts as the last word of a
    line in the right part of the page.
    """

    found: list[ReferenceTarget] = []
    seen_blocks: set[int] = set()
    lines = _lines(words)
    # A contents page lists "Chapter 3 ..... 41" / "3.2 Title 45": its headings are not the statements.
    numbered_ends = sum(1 for line in lines if _normal(str(line[-1][4])).strip(".").isdigit() and len(line) >= 2)
    contents_page = len(lines) >= 6 and numbered_ends >= 0.4 * len(lines)
    for line in lines:
        first = line[0]
        block = int(first[5])
        first_of_block = block not in seen_blocks
        seen_blocks.add(block)
        glued = _GLUED_HEADING_RE.match(_normal(str(first[4])))
        if first_of_block and glued and _kind(glued.group(1)) and not contents_page:
            # OCR of a scan: "Theorem4.6.3LetR..." as one word.
            found.append(ReferenceTarget(_kind(glued.group(1)), glued.group(2), page_index, float(first[1]), float(first[0]), _line_text(line)))
        elif first_of_block and len(line) >= 2 and not contents_page:
            kind = _kind(str(first[4]))
            number = _number(str(line[1][4]))
            # "Chapter 1 introduces ..." opening a paragraph is prose; a heading line is short.
            heading_only = kind in {"chapter", "section", "appendix"}
            if kind and number and (not heading_only or len(line) <= 8):
                found.append(ReferenceTarget(kind, number, page_index, float(first[1]), float(first[0]), _line_text(line)))
            elif _SECTION_HEADING_RE.match(_normal(str(first[4])).rstrip(".")) and str(line[1][4])[:1].isupper() and len(line) <= 10:
                found.append(ReferenceTarget("section", _normal(str(first[4])).rstrip("."), page_index, float(first[1]), float(first[0]), _line_text(line)))
        if first_of_block and not contents_page:
            token = _normal(str(first[4]))
            if token.startswith("§") and _number(token[1:]):
                found.append(ReferenceTarget("section", _number(token[1:]), page_index, float(first[1]), float(first[0]), _line_text(line)))
        last = line[-1]
        equation = _equation(str(last[4]))
        # A tag stands apart at the right margin (often its own text line);
        # "... by (2.14)" ending a wrapped line sits right after the previous word.
        apart = len(line) == 1 or float(last[0]) - float(line[-2][2]) > 24
        if equation and float(last[0]) > page_width * 0.62 and apart:
            found.append(ReferenceTarget("equation", equation, page_index, float(last[1]), float(last[0]), _line_text(line[:-1])))
    return found


def reference_at(words: Sequence[tuple], x: float, y: float) -> tuple[str, str] | None:
    """The reference under the pointer: ``("theorem", "3.2")``, ``("equation", "2.14")``..."""

    for index, word in enumerate(words):
        x0, y0, x1, y1 = (float(value) for value in word[:4])
        if not (x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1):
            continue
        token = str(word[4])
        equation = _equation(token)
        if equation:
            return "equation", equation
        if token.startswith("§") and _number(token[1:]):
            return "section", _number(token[1:])
        glued = _GLUED_HEADING_RE.match(_normal(token))
        if glued and _kind(glued.group(1)):
            return _kind(glued.group(1)), glued.group(2)
        number = _number(token)
        if number and index > 0:
            kind = _kind(str(words[index - 1][4]))
            if kind:
                return kind, number
        kind = _kind(token)
        if kind and index + 1 < len(words):
            number = _number(str(words[index + 1][4]))
            if number:
                return kind, number
        return None
    return None


@dataclass
class ReferenceIndex:
    """Statements of the numbered items in a document, in page order."""

    targets: dict[tuple[str, str], list[ReferenceTarget]] = field(default_factory=dict)

    def add_page(self, page_index: int, words: Sequence[tuple], page_width: float) -> None:
        for target in reference_definitions(page_index, words, page_width):
            self.targets.setdefault((target.kind, target.number), []).append(target)

    @staticmethod
    def _choose(candidates: list[ReferenceTarget], from_page: int | None) -> ReferenceTarget:
        # Books that restart numbering in each chapter state "(1.1)" once per
        # chapter; the one cited is the nearest statement before the citation.
        if from_page is None or len(candidates) == 1:
            return candidates[0]
        before = [target for target in candidates if target.page <= from_page]
        return before[-1] if before else candidates[0]

    def find(self, kind: str, number: str, from_page: int | None = None) -> ReferenceTarget | None:
        candidates = self.targets.get((kind, number))
        if not candidates and kind in {"theorem", "lemma", "proposition", "corollary", "definition", "example", "remark", "claim", "fact"}:
            # Books number theorems, lemmas... in one shared sequence, and some
            # authors cite "Theorem 3.2" for what the book heads "Proposition 3.2".
            for other_kind in ("theorem", "proposition", "lemma", "corollary", "definition", "example", "remark"):
                candidates = self.targets.get((other_kind, number))
                if candidates:
                    break
        return self._choose(candidates, from_page) if candidates else None

    def statements(self) -> list[ReferenceTarget]:
        """Every stated item for the 定理 list, in reading order (sections and chapters excluded)."""

        items = [target for targets in self.targets.values() for target in targets if target.kind in STATEMENT_GROUPS]
        return sorted(items, key=lambda target: (target.page, target.y, target.x))

    @classmethod
    def build(cls, document: Any) -> "ReferenceIndex":
        index = cls()
        for page_index in range(int(document.page_count)):
            page = document.load_page(page_index)
            index.add_page(page_index, page_words(page), float(page.rect.width))
        return index


def preview_clip(blocks: Sequence[Sequence[Any]], page_width: float, page_height: float, y: float, *, kind: str = "") -> Rect:
    """The region of a target page shown in the hover preview.

    Starts at the destination and takes whole text blocks (a theorem's statement,
    an equation and the line around it) until about a third of a page.  A figure
    caption is preceded by the figure itself, so its preview reaches upward.
    """

    text_blocks = sorted(
        (tuple(float(value) for value in block[:4]) for block in blocks if len(block) >= 5 and (len(block) < 7 or int(block[6]) == 0)),
        key=lambda rect: (rect[1], rect[0]),
    )
    left = min((rect[0] for rect in text_blocks), default=36.0)
    right = max((rect[2] for rect in text_blocks), default=page_width - 36.0)
    left, right = max(0.0, left - 6), min(page_width, right + 6)
    if kind == "figure":
        top = max(0.0, y - 260)
        return (left, top, right, min(page_height, y + 60))
    start = max(0.0, y - 8)
    limit = 120.0 if kind == "equation" else 230.0
    top: float | None = None
    bottom = start
    for x0, y0, x1, y1 in text_blocks:
        if y1 <= start + 2:
            continue
        if top is None:
            top = min(start, y0)
        elif y0 - top > limit:
            break
        bottom = max(bottom, y1)
        if bottom - top >= limit * 0.6:
            break
    if top is None:
        top = start
    bottom = min(max(bottom, top + 40), top + limit + 60, page_height)
    return (left, max(0.0, top - 4), right, min(page_height, bottom + 4))


# -- page labels ----------------------------------------------------------------


def document_page_labels(document: Any) -> list[str]:
    """Printed page labels ("i", "xii", "1", "A-3"), or [] when the PDF defines none."""

    try:
        if not document.get_page_labels():
            return []
    except Exception:
        return []
    labels: list[str] = []
    for page_index in range(int(document.page_count)):
        try:
            labels.append(str(document.load_page(page_index).get_label() or ""))
        except Exception:
            labels.append("")
    return labels if any(labels) else []


def printed_label(page_index: int, labels: Sequence[str], offset: int | None) -> str:
    """The printed page number shown for a page; '' when it is just the PDF page number."""

    if offset is not None:
        number = page_index + 1 - offset
        return str(number) if number >= 1 else ""
    if 0 <= page_index < len(labels) and labels[page_index] and labels[page_index] != str(page_index + 1):
        return labels[page_index]
    return ""


def resolve_page_input(text: str, *, page_count: int, labels: Sequence[str] = (), offset: int | None = None) -> int | None:
    """Turn what the user typed in the page box into a 0-based PDF page.

    ``p123`` / ``p.123`` / ``第123页`` and plain labels such as ``xii`` mean the
    printed page; ``#12`` means the 12th page of the PDF file.  A plain number
    is the printed page when the book has page labels or a set offset, and the
    PDF page otherwise.
    """

    value = _normal(text).strip()
    if not value or page_count <= 0:
        return None

    def clamp(index: int) -> int | None:
        return index if 0 <= index < page_count else None

    physical = re.fullmatch(r"#\s*(\d+)", value)
    if physical:
        return clamp(int(physical.group(1)) - 1)
    printed = re.fullmatch(r"(?:p\.?|第)\s*([0-9A-Za-z.\-]+)\s*页?", value, flags=re.IGNORECASE)
    wanted = printed.group(1) if printed else value
    if offset is not None and wanted.isdigit():
        return clamp(int(wanted) - 1 + offset)
    if labels:
        lowered = wanted.lower()
        for index, label in enumerate(labels):
            if label and label.lower() == lowered:
                return index
    if wanted.isdigit():
        return clamp(int(wanted) - 1)
    return None


# -- code blocks and tables -----------------------------------------------------------------

MONO_FONT_RE = re.compile(
    r"mono|courier|consol|menlo|cmtt|lmtt|txtt|sfmono|inconsolata|sourcecode|firacode|typewriter|beramono|nimbusmon|lucidaconsole",
    re.IGNORECASE,
)


def is_code_selection(glyphs: Sequence[Any]) -> bool:
    """Mostly typewriter glyphs: a code listing, copied with its indentation."""

    visible = [glyph for glyph in glyphs if str(glyph.text).strip()]
    if len(visible) < 2:
        return False
    mono = sum(1 for glyph in visible if MONO_FONT_RE.search(str(glyph.font_name or "")))
    return mono >= 0.7 * len(visible)


def code_text(lines: Sequence[Sequence[Any]]) -> str:
    """Rebuild code from monospaced glyph lines, columns taken from glyph positions.

    PDF text drops leading spaces, so indentation (which carries meaning in
    Python and is the structure of Verilog/C listings) is recovered by placing
    each glyph at ``round((x - left) / character width)``.
    """

    visible = [glyph for line in lines for glyph in line if str(glyph.text).strip()]
    if not visible:
        return ""
    widths = sorted(float(glyph.raw_bbox[2]) - float(glyph.raw_bbox[0]) for glyph in visible)
    width = max(0.1, widths[len(widths) // 2])
    left = min(float(glyph.raw_bbox[0]) for glyph in visible)
    output: list[str] = []
    for line in lines:
        cells: dict[int, str] = {}
        for glyph in line:
            text = str(glyph.text)
            if not text.strip():
                continue
            column = max(0, round((float(glyph.raw_bbox[0]) - left) / width))
            while column in cells:
                column += 1
            cells[column] = text
        if not cells:
            output.append("")
            continue
        row = [" "] * (max(cells) + 1)
        for column, text in cells.items():
            row[column] = text
        output.append("".join(row).rstrip())
    return "\n".join(output)


def table_rows(page: Any, rect: Rect) -> list[list[str]]:
    """Cells of the table inside ``rect``: ruled tables first, then text-aligned ones."""

    fitz = _fitz()
    clip = fitz.Rect(*rect)
    for options in ({}, {"vertical_strategy": "text", "horizontal_strategy": "text"}):
        try:
            found = page.find_tables(clip=clip, **options)
        except Exception:
            continue
        tables = list(getattr(found, "tables", None) or [])
        if not tables:
            continue
        table = max(tables, key=lambda item: len(item.extract() or []))
        rows = [[" ".join(str(cell or "").split()) for cell in row] for row in table.extract() or []]
        rows = [row for row in rows if any(row)]
        if rows and max(len(row) for row in rows) >= 2:
            return rows
    return []


def rows_to_tsv(rows: Sequence[Sequence[str]]) -> str:
    return "\n".join("\t".join(cell.replace("\t", " ") for cell in row) for row in rows)
