"""Structured parser for the TeX source of 《旋元佑英文写作》.

The book has three parts (错误分析, 章法结构, 句法修辞) and an appendix.  A
chapter is a lecture made of numbered ``\\section``\\ s — example boxes
(``writingwrong`` / ``writingcorrect`` / ``writingquestion``), model essays
(``writingessay``), outline trees, transition-word tables, sentence patterns —
and, in most chapters, a closing ``writingexercise`` with its
``writinganswers``.

Only the closing exercises go into the question bank.  Everything else —
including the essay prompts of the 考古题 / 模拟题 sections, which the book sets
in the running text — is lecture material for the 写作方法 page.

The parser works like the reading book's: :func:`scan_blocks` turns TeX into
a list of block events; layout-only commands are dropped by an explicit list
and any other unknown command is an error, so a source change can never leak
TeX into the program.  The assemblers then build the method sections and the
exercise groups, pairing every exercise item with its answer.

Rich text is stored as *runs* (as in the other two books): ``{"t": text}``
plus style keys ``b``, ``i``, ``u`` (underline), ``g`` (the book's blue key
words), ``hl`` (a pattern slot such as X), ``sup``; and the special runs
``{"br": True}``, ``{"line": True}``, ``{"blank": True}`` and
``{"circ": "1"}`` (an enclosed number).  The low-level TeX readers and the
run helpers are shared with :mod:`raydu_reading_parser`.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from pathlib import Path
from typing import Any, Iterable

from shared.scripts.raydu_reading_parser import (
    TexParseError,
    _find_environment_end,
    _read_args,
    _read_group,
    _read_optional,
    _search,
    _skip_spaces,
    _squash,
    normalize_runs,
    runs_to_text,
    strip_comments,
)

QUESTION_CODE_PREFIX = "XYWR"
BOOK_TITLE = "旋元佑英文写作"
PDF_NAME = "旋元佑英文写作.pdf"
PART_TITLES = {1: "错误分析", 2: "章法结构", 3: "句法修辞", 4: "附录"}
PART_NUMERALS = {1: "第一部分", 2: "第二部分", 3: "第三部分", 4: "附录"}

TYPE_NAMES = {
    "fill_blank": "填空",
    "correction": "改错题",
    "rewrite": "改写",
    "combine": "句子组合",
    "punctuation": "标点",
    "writing": "写作",
}
# How much the learner writes: the size of the answer box.
ANSWER_SIZES = ("sentence", "paragraph", "essay")


# ---------------------------------------------------------------------------
# Inline rich text

_IGNORED_INLINE = {
    "noindent", "smallskip", "medskip", "bigskip", "nopagebreak", "centering", "raggedright", "raggedleft",
    "clearpage", "newpage", "phantomsection", "normalfont", "normalsize", "small", "large", "rmfamily",
    "sffamily", "bfseries", "relax", "strut", "allowbreak", "WritingEnumFont", "linewidth", "hline",
    "indent", "unskip", "ignorespaces",
}
_SYMBOLS = {
    "WritingApostrophe": "’", "WritingMiddleDot": "·", "textellipsis": "…", "ldots": "…", "dots": "…",
    "textquoteright": "’", "textquoteleft": "‘", "textquotedblleft": "“", "textquotedblright": "”",
    "textemdash": "—", "textendash": "–", "textbullet": "•", "WritingOutlineBullet": "•",
    "WritingOutlineArrow": "↳", "rightarrow": "→", "times": "×", "textperiodcentered": "·",
}
_SPACES = {"quad": "\u2003", "qquad": "\u2003\u2003", "enspace": " ", "thinspace": " ", "space": " ",
           "hfill": " "}
_STYLES = {
    "textbf": {"b": True}, "textit": {"i": True}, "emph": {"i": True}, "textsuperscript": {"sup": True},
    "WritingUnderline": {"u": True}, "CJKunderline": {"u": True}, "uline": {"u": True},
    "underline": {"u": True}, "WritingBlue": {"g": True}, "WritingSlot": {"hl": True},
    "mbox": {}, "text": {}, "textrm": {}, "textnormal": {},
}
_ESCAPED = {"%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", " ": " ", ",": "\u2009"}
# Layout commands dropped with their arguments: name -> number of arguments.
_DROP_ARGS = {
    "setlength": 2, "addtolength": 2, "markboth": 2, "addcontentsline": 3, "hspace": 1, "vspace": 1,
    "Needspace": 1, "needspace": 1, "addvspace": 1, "label": 1, "thispagestyle": 1, "pagestyle": 1,
}
_DROP_ASSIGN = {"tabcolsep", "parindent", "parskip", "arraystretch", "baselineskip", "hangindent", "hangafter"}


def parse_inline(source: str) -> list[dict[str, Any]]:
    """Convert inline TeX of the writing book into style runs."""

    runs: list[dict[str, Any]] = []

    def walk(text: str, style: dict[str, Any]) -> None:
        index = 0
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                runs.append({"t": "".join(buffer), **style})
                buffer.clear()

        while index < len(text):
            character = text[index]
            if character == "\\":
                match = re.match(r"\\([A-Za-z@]+)\*?", text[index:])
                if match is None:
                    following = text[index + 1:index + 2]
                    if following == "\\":
                        flush()
                        runs.append({"line": True})
                        index = _skip_spaces(text, index + 2)
                        if index < len(text) and text[index] == "[":
                            _value, index = _read_optional(text, index)
                        continue
                    if following in _ESCAPED:
                        buffer.append(_ESCAPED[following])
                        index += 2
                        continue
                    raise TexParseError(f"无法识别的转义：\\{following}")
                name = match.group(1)
                index += match.end()
                if name in _IGNORED_INLINE:
                    continue
                if name in _SYMBOLS:
                    buffer.append(_SYMBOLS[name])
                    if text.startswith("{}", index):
                        index += 2
                    continue
                if name in _SPACES:
                    buffer.append(_SPACES[name])
                    continue
                if name == "par":
                    flush()
                    runs.append({"br": True})
                    continue
                if name == "newline":
                    flush()
                    runs.append({"line": True})
                    continue
                if name in _DROP_ARGS:
                    index = _skip_spaces(text, index)
                    if text.startswith("*", index):
                        index += 1
                    _values, index = _read_args(text, index, _DROP_ARGS[name])
                    if name == "hspace":
                        buffer.append(" ")
                    continue
                if name in _DROP_ASSIGN:
                    skip = re.match(r"\s*=?\s*-?[\d.]*\s*\\?[A-Za-z]*", text[index:])
                    index += skip.end() if skip else 0
                    continue
                if name == "texorpdfstring":
                    (_tex, plain), index = _read_args(text, index, 2)
                    flush()
                    walk(plain, style)
                    continue
                if name == "WritingSymbol":
                    (symbol,), index = _read_args(text, index, 1)
                    flush()
                    runs.append({"t": _plain(symbol), "g": True, "b": True})
                    buffer.append(" ")
                    continue
                if name in {"WritingEnum", "WritingCircled"}:
                    (number,), index = _read_args(text, index, 1)
                    flush()
                    runs.append({"circ": _plain(number)})
                    continue
                if name == "WritingBlank":
                    flush()
                    runs.append({"blank": True})
                    if text.startswith("{}", index):
                        index += 2
                    continue
                if name == "textcolor":
                    (_color, content), index = _read_args(text, index, 2)
                    flush()
                    walk(content, {**style, "g": True})
                    continue
                if name in _STYLES:
                    if name == "CJKunderline":
                        _optional, index = _read_optional(text, index)
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(content, {**style, **_STYLES[name]})
                    continue
                raise TexParseError(f"无法识别的命令：\\{name}")
            if character == "{":
                content, index = _read_group(text, index)
                flush()
                walk(content, dict(style))
                continue
            if character == "}":
                raise TexParseError("多余的右花括号")
            if character == "$":
                raise TexParseError("写作书中不应出现数学模式 $")
            if character == "~":
                buffer.append("\u00a0")
            elif text.startswith("``", index):
                buffer.append("“")
                index += 2
                continue
            elif text.startswith("''", index):
                buffer.append("”")
                index += 2
                continue
            elif text.startswith("---", index):
                buffer.append("—")
                index += 3
                continue
            elif text.startswith("--", index):
                buffer.append("–")
                index += 2
                continue
            elif character == "\n" and re.match(r"\n[ \t]*\n", text[index:]):
                flush()
                runs.append({"br": True})
                index = _skip_spaces(text, index)
                continue
            elif character in "\r\n\t":
                buffer.append(" ")
            else:
                buffer.append(character)
            index += 1
        flush()

    walk(str(source or ""), {})
    return normalize_runs(runs)


def _plain(source: str) -> str:
    return runs_to_text(parse_inline(source)).strip()


_CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")


def is_chinese(runs: Iterable[dict[str, Any]]) -> bool:
    """Whether a run list is Chinese text (a translation) rather than English."""

    text = runs_to_text(runs)
    letters = sum(1 for character in text if character.isalpha())
    return bool(letters) and len(_CJK.findall(text)) * 3 >= letters


def split_paragraphs(runs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    paragraphs: list[list[dict[str, Any]]] = [[]]
    for run in runs:
        if run.get("br"):
            paragraphs.append([])
        else:
            paragraphs[-1].append(run)
    return [normalize_runs(paragraph) for paragraph in paragraphs if normalize_runs(paragraph)]


def split_lines(runs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = [[]]
    for run in runs:
        if run.get("br") or run.get("line"):
            lines.append([])
        else:
            lines[-1].append(run)
    return [normalize_runs(line) for line in lines if normalize_runs(line)]


# ---------------------------------------------------------------------------
# Block events

# Block macros: name -> (number of arguments, event kind).  ``\WritingSpeaker``
# also takes an optional column width.
_BLOCK_MACROS = {
    "chapter": (1, "chapter"),
    "section": (1, "section"),
    "subsection": (1, "subsection"),
    "WritingTranslationLine": (1, "translation"),
    "WritingTag": (2, "tag"),
    "WritingLabel": (1, "label"),
    "WritingPhraseHeading": (1, "phrase"),
    "WritingExerciseHeading": (3, "exercise_heading"),
    "WritingExerciseInstruction": (2, "exercise_instruction"),
    "WritingAnswerHeading": (1, "answer_heading"),
    "WritingExerciseItem": (2, "exercise_item"),
    "WritingAnswerItem": (2, "answer_item"),
    "WritingExampleSource": (1, "source_line"),
    "WritingSpeaker": (2, "speaker"),
    "WritingPlainItem": (2, "plain_example"),
    "WritingEssaySource": (1, "essay_source"),
    "WritingEssayNote": (1, "essay_note"),
    "WritingEssayTranslation": (1, "essay_translation"),
    "WritingEssayRule": (0, "essay_rule"),
    "WritingOutlineLine": (3, "outline_line"),
    "WritingPatternGap": (0, "pattern_gap"),
    "item": (0, "item"),
}
_EXAMPLE_STYLES = {"writingwrong": "wrong", "writingcorrect": "correct", "writingquestion": "question"}
_NESTED_ENVS = {
    "solution": "solution", "writingcitation": "citation", "writingprompt": "prompt",
    "writingexampleplain": "plain_examples",
}
_REGION_ENVS = {"writingexercise": "exercise_region", "writingexercisecontinued": "exercise_region",
                "writinganswers": "answers_region", "writinganswerscontinued": "answers_region"}
_TABLE_ENVS = {"tabular", "writingtermtable"}


def _contains_block(text: str) -> bool:
    return bool(re.search(r"\\(?:begin\{|" + "|".join(sorted(_BLOCK_MACROS, key=len, reverse=True)) + r")\b", text))


class _Lines:
    def __init__(self, text: str, offset: int) -> None:
        self.starts = [0] + [match.end() for match in re.finditer("\n", text)]
        self.offset = offset

    def __call__(self, position: int) -> int:
        return self.offset + bisect_right(self.starts, position)


def scan_blocks(text: str, *, line_offset: int = 0) -> list[dict[str, Any]]:
    """Turn TeX into a list of block events (paragraphs, boxes, headings, items...)."""

    events: list[dict[str, Any]] = []
    buffer: list[str] = []
    buffer_start = [0]
    line_of = _Lines(text, line_offset)
    index = 0

    def flush() -> None:
        raw = "".join(buffer)
        buffer.clear()
        if not raw.strip():
            return
        try:
            runs = parse_inline(raw)
        except TexParseError as error:
            raise TexParseError(f"第 {line_of(buffer_start[0])} 行：{error}") from None
        for paragraph in split_paragraphs(runs):
            events.append({"k": "para", "runs": paragraph, "line": line_of(buffer_start[0])})

    def append(chunk: str, position: int) -> None:
        if not buffer or not "".join(buffer).strip():
            buffer_start[0] = position
        buffer.append(chunk)

    def inline(source: str, position: int) -> list[dict[str, Any]]:
        try:
            return parse_inline(source)
        except TexParseError as error:
            raise TexParseError(f"第 {line_of(position)} 行：{error}") from None

    while index < len(text):
        character = text[index]
        if character == "\n" and re.match(r"\n[ \t]*\n", text[index:]):
            flush()
            index = _skip_spaces(text, index)
            continue
        if character == "{":
            content, end = _read_group(text, index)
            stripped = content.lstrip()
            if stripped.startswith("\\centering"):
                # {\centering\textbf{Title}\par}: an essay title.
                flush()
                events.append({"k": "title", "text": runs_to_text(inline(stripped[len("\\centering"):], index)),
                               "line": line_of(index)})
            elif _contains_block(content):
                flush()
                events.extend(scan_blocks(content, line_offset=line_of(index) - 1))
            else:
                append(text[index:end], index)
            index = end
            continue
        if character != "\\":
            append(character, index)
            index += 1
            continue
        match = re.match(r"\\([A-Za-z@]+)(\*?)", text[index:])
        if match is None:
            append(text[index:index + 2], index)
            index += 2
            continue
        name, star = match.group(1), match.group(2)
        after = index + match.end()
        position = index
        if name == "begin":
            (environment,), body_start = _read_args(text, after, 1)
            environment = environment.strip()
            end_start, end_after = _find_environment_end(text, environment, body_start)
            body = text[body_start:end_start]
            flush()
            events.append(_environment_event(environment, body, line_of(position), inline))
            index = end_after
            continue
        if name in _BLOCK_MACROS:
            count, kind = _BLOCK_MACROS[name]
            short = None
            cursor = after
            if name in {"section", "subsection", "WritingSpeaker"}:
                short, cursor = _read_optional(text, cursor)
            args, index = _read_args(text, cursor, count) if count else ([], cursor)
            flush()
            event: dict[str, Any] = {"k": kind, "line": line_of(position)}
            if kind in {"chapter", "section", "subsection"}:
                event["title"] = runs_to_text(inline(short if short is not None else args[0], position))
                event["full_title"] = runs_to_text(inline(args[0], position))
                event["numbered"] = not star
            elif kind == "answer_item":
                event["number"] = _plain(args[0])
                event["blocks"] = scan_blocks(args[1], line_offset=line_of(position) - 1)
            elif kind == "exercise_item":
                event["number"] = _plain(args[0])
                event["runs"] = inline(args[1], position)
            elif kind == "exercise_heading":
                event["letter"] = _plain(args[0])
                event["en"], event["zh"] = inline(args[1], position), inline(args[2], position)
            elif kind == "exercise_instruction":
                event["en"], event["zh"] = inline(args[0], position), inline(args[1], position)
            elif kind == "answer_heading":
                event["letter"] = _plain(args[0])
            elif kind in {"label", "essay_translation"}:
                event["text"] = runs_to_text(inline(args[0], position))
            elif kind in {"tag", "speaker", "plain_example"}:
                event["first"], event["runs"] = inline(args[0], position), inline(args[1], position)
            elif kind == "outline_line":
                width = re.match(r"\s*([\d.]+)\s*(em|pt)", args[0])
                indent = float(width.group(1)) / (10 if width and width.group(2) == "pt" else 1) if width else 0.0
                event.update(indent=indent, mark=inline(args[1], position), runs=inline(args[2], position))
            elif count:
                event["runs"] = inline(args[0], position)
            events.append(event)
            continue
        append(text[index:after], index)
        index = after
    flush()
    return events


def _environment_event(environment: str, body: str, line: int, inline: Any) -> dict[str, Any]:
    if environment in _EXAMPLE_STYLES:
        return {"k": "example", "style": _EXAMPLE_STYLES[environment], "events": scan_blocks(body, line_offset=line - 1),
                "line": line}
    if environment in _NESTED_ENVS:
        return {"k": _NESTED_ENVS[environment], "events": scan_blocks(body, line_offset=line - 1), "line": line}
    if environment in _REGION_ENVS:
        return {"k": _REGION_ENVS[environment], "events": scan_blocks(body, line_offset=line - 1), "line": line}
    if environment == "writingessay":
        return {"k": "essay", "events": scan_blocks(body, line_offset=line - 1), "line": line}
    if environment == "writingreading":
        return {"k": "reading", "runs": inline(body, 0), "line": line}
    if environment in {"writingoutline", "writingoutlineplain"}:
        events = scan_blocks(body, line_offset=line - 1)
        stray = [event["k"] for event in events if event["k"] not in {"outline_line", "para"}]
        if stray:
            raise TexParseError(f"第 {line} 行：大纲中出现了大纲行以外的内容：{', '.join(stray)}")
        # A paragraph inside an outline is a category heading such as 缺点 (Drawbacks).
        lines = [event if event["k"] == "outline_line" else {"heading": event["runs"]} for event in events]
        return {"k": "outline", "framed": environment == "writingoutline", "lines": lines, "line": line}
    if environment == "writingpatterns":
        _width, cursor = _read_optional(body, 0)
        body = body[cursor:]
        return {"k": "patterns", "events": scan_blocks(body, line_offset=line - 1), "line": line}
    if environment in _TABLE_ENVS:
        (_spec,), position = _read_args(body, 0, 1)
        rows = []
        for raw_row in re.split(r"\\\\(?:\s*\\hline)?", body[position:]):
            if not raw_row.strip() or raw_row.strip() == "\\hline":
                continue
            rows.append([inline(cell, 0) for cell in _split_cells(raw_row.replace("\\hline", ""))])
        return {"k": "table", "style": "term" if environment == "writingtermtable" else "plain", "rows": rows,
                "line": line}
    raise TexParseError(f"第 {line} 行：无法识别的环境 {environment}")


def _split_cells(text: str) -> list[str]:
    cells, depth, start = [], 0, 0
    for position, character in enumerate(text):
        if character == "{" and text[position - 1:position] != "\\":
            depth += 1
        elif character == "}" and text[position - 1:position] != "\\":
            depth -= 1
        elif character == "&" and depth == 0 and text[position - 1:position] != "\\":
            cells.append(text[start:position])
            start = position + 1
    cells.append(text[start:])
    return cells


# ---------------------------------------------------------------------------
# Display blocks

def to_blocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Events (without exercise regions or section headings) as display blocks."""

    blocks: list[dict[str, Any]] = []
    for event in events:
        kind = event["k"]
        if kind == "para":
            blocks.append({"kind": "para", "runs": event["runs"]})
        elif kind == "subsection" or (kind == "section" and not event.get("numbered")):
            blocks.append({"kind": "heading", "text": event["title"]})
        elif kind == "title":
            blocks.append({"kind": "title", "text": event["text"]})
        elif kind == "example":
            blocks.append({"kind": "example", "style": event["style"], "blocks": to_blocks(event["events"])})
        elif kind in {"solution", "citation", "prompt"}:
            blocks.append({"kind": kind, "blocks": to_blocks(event["events"])})
        elif kind == "plain_examples":
            blocks.extend(to_blocks(event["events"]))
        elif kind == "reading":
            blocks.append({"kind": "reading", "runs": event["runs"]})
        elif kind == "essay":
            blocks.append(_essay_block(event["events"]))
        elif kind == "outline":
            blocks.append({"kind": "outline", "framed": event["framed"], "lines": [
                {"heading": line["heading"]} if "heading" in line
                else {"indent": line["indent"], "mark": line["mark"], "runs": line["runs"]}
                for line in event["lines"]
            ]})
        elif kind == "patterns":
            groups: list[list[list[dict[str, Any]]]] = [[]]
            for item in event["events"]:
                if item["k"] == "pattern_gap":
                    groups.append([])
                elif item["k"] == "item":
                    groups[-1].append([])
                elif item["k"] == "para" and groups[-1]:
                    groups[-1][-1] = normalize_runs([*groups[-1][-1], *([{"line": True}] if groups[-1][-1] else []),
                                                     *item["runs"]])
                else:
                    raise TexParseError(f"第 {item['line']} 行：句型列表中出现了无法归属的内容")
            blocks.append({"kind": "patterns", "groups": [group for group in groups if group]})
        elif kind == "table":
            blocks.append({"kind": "table", "style": event["style"], "rows": event["rows"]})
        elif kind == "translation":
            blocks.append({"kind": "translation", "runs": event["runs"]})
        elif kind == "tag":
            blocks.append({"kind": "tag", "label": runs_to_text(event["first"]), "runs": event["runs"]})
        elif kind == "label":
            blocks.append({"kind": "label", "text": event["text"]})
        elif kind == "phrase":
            blocks.append({"kind": "phrase", "runs": event["runs"]})
        elif kind == "plain_example":
            blocks.append({"kind": "plain_example", "en": event["first"], "zh": event["runs"]})
        elif kind == "speaker":
            blocks.append({"kind": "speaker", "speaker": event["first"], "runs": event["runs"]})
        elif kind == "source_line":
            blocks.append({"kind": "source_line", "runs": event["runs"]})
        elif kind == "essay_note":
            blocks.append({"kind": "para", "runs": event["runs"]})
        elif kind == "essay_source":
            # Outside an essay box it is a bold source line (the appendix's reading sites).
            blocks.append({"kind": "para", "runs": event["runs"], "bold": True})
        else:
            raise TexParseError(f"第 {event.get('line', '?')} 行：讲解中出现了无法显示的内容：{kind}")
    return blocks


def _essay_block(events: list[dict[str, Any]]) -> dict[str, Any]:
    """A model essay: source line, the English text, then (folded) the translation."""

    source: list[dict[str, Any]] = []
    english: list[dict[str, Any]] = []
    translation: list[dict[str, Any]] = []
    translation_title = ""
    started = False
    for event in events:
        if event["k"] == "essay_source" and not started and not english:
            source = event["runs"]
            continue
        if event["k"] in {"essay_translation", "essay_rule"}:
            if started:
                translation.append({"kind": "rule"})
            started = True
            if event["k"] == "essay_translation":
                if translation_title or translation:
                    translation.append({"kind": "heading", "text": event["text"]})
                else:
                    translation_title = event["text"]
            continue
        if event["k"] == "essay_source":
            (translation if started else english).append({"kind": "para", "runs": event["runs"], "bold": True})
            continue
        (translation if started else english).extend(to_blocks([event]))
    return {"kind": "essay", "source": source, "blocks": english, "translation": translation,
            "translation_title": translation_title}


def blocks_to_runs(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display blocks as one run list (an answer, a translation or an explanation)."""

    runs: list[dict[str, Any]] = []
    previous = [""]

    def add(part: list[dict[str, Any]]) -> None:
        if part:
            if runs:
                # The 主题 / 立场 / 发展 rows of a thesis analysis are lines of one list.
                runs.append({"line": True} if previous[0] == "tag" == kind else {"br": True})
            runs.extend(part)
            previous[0] = kind

    for block in blocks:
        kind = block["kind"]
        if kind in {"para", "translation", "reading", "source_line", "phrase"}:
            add([{**run, "b": True} if block.get("bold") and "t" in run else run for run in block["runs"]])
        elif kind in {"title", "heading", "label"}:
            add([{"t": block["text"], "b": True}])
        elif kind == "tag":
            add([{"t": f"{block['label']}\u2003", "b": True}, *block["runs"]])
        elif kind == "speaker":
            add([*block["speaker"], {"t": " "}, *block["runs"]])
        elif kind == "plain_example":
            add([{"t": "▪ ", "g": True}, *block["en"], {"line": True}, *block["zh"]])
        elif kind in {"example", "solution", "citation", "prompt"}:
            add(blocks_to_runs(block["blocks"]))
        elif kind == "essay":
            add(block["source"] and [{**run, "b": True} for run in block["source"]])
            add(blocks_to_runs(block["blocks"]))
            if block["translation_title"]:
                add([{"t": block["translation_title"], "b": True}])
            add(blocks_to_runs(block["translation"]))
        elif kind == "outline":
            lines: list[dict[str, Any]] = []
            for line in block["lines"]:
                if lines:
                    lines.append({"line": True})
                if "heading" in line:
                    lines.extend({**run, "b": True} if "t" in run else run for run in line["heading"])
                    continue
                lines.append({"t": "\u2003" * int(round(line["indent"] / 1.5))})
                lines.extend(line["mark"])
                lines.append({"t": " "})
                lines.extend(line["runs"])
            add(lines)
        elif kind == "patterns":
            items: list[dict[str, Any]] = []
            for group in block["groups"]:
                for item in group:
                    if items:
                        items.append({"line": True})
                    items.extend([{"t": "▪ ", "g": True}, *item])
            add(items)
        elif kind == "table":
            add([{"table": block["rows"]}])
    return normalize_runs(runs)


# ---------------------------------------------------------------------------
# The book: chapters, method sections and exercises

def chapter_order(book_root: Path) -> list[dict[str, Any]]:
    """The chapter files in the order of the main TeX file, with their part."""

    main = book_root / f"{BOOK_TITLE}.tex"
    text = strip_comments(main.read_text(encoding="utf-8"))
    chapters: list[dict[str, Any]] = []
    part = 0
    for match in re.finditer(r"\\part\{([^}]*)\}|\\input\{chapters/([^}]+)\}", text):
        if match.group(1) is not None:
            part += 1
            continue
        stem = match.group(2)
        found = re.fullmatch(r"part(\d)-chapter-(\d+)", stem)
        number = int(found.group(2)) if found else 1
        if found and int(found.group(1)) != part:
            raise TexParseError(f"主文件中 {stem} 位于第 {part} 部分之下")
        chapters.append({"stem": stem, "part": part, "chapter": number})
    return chapters


def chapter_key(part: int, chapter: int) -> str:
    return f"{QUESTION_CODE_PREFIX}-{part}-{chapter:02d}"


def parse_book(book_root: Path | str) -> dict[str, Any]:
    """Method sections, exercise groups and questions of the whole book."""

    root = Path(book_root)
    chapters: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[str] = []
    for spec in chapter_order(root):
        source_file = root / "chapters" / f"{spec['stem']}.tex"
        try:
            events = scan_blocks(strip_comments(source_file.read_text(encoding="utf-8")))
            built = _assemble_chapter(spec, source_file.name, events)
        except (OSError, TexParseError) as error:
            issues.append(f"{source_file.name}：{error}")
            continue
        chapters.append(built["chapter"])
        sections.extend(built["sections"])
        groups.extend(built["groups"])
        questions.extend(built["questions"])
        issues.extend(f"{source_file.name}：{problem}" for problem in built["issues"])
        warnings.extend(f"{source_file.name}：{warning}" for warning in built["warnings"])
    for kind, codes in (("题号", [item["question_code"] for item in questions]),
                        ("题组编号", [group["group_code"] for group in groups])):
        for code in sorted({code for code in codes if codes.count(code) > 1}):
            issues.append(f"{code}：{kind}重复，重新导入时会互相覆盖")
    for section in sections:
        residues = text_residues(section["blocks"])
        if residues:
            issues.append(f"{section['source_file']}（{section['title']}）：残留 TeX 片段：" + " ".join(residues))
    for item in [*groups, *questions]:
        residues = text_residues({key: value for key, value in item.items() if key != "warnings"})
        if residues:
            issues.append(f"{item.get('question_code') or item['group_code']}：残留 TeX 片段：" + " ".join(residues))
    return {"chapters": chapters, "sections": sections, "groups": groups, "questions": questions,
            "issues": issues, "warnings": warnings}


def _assemble_chapter(spec: dict[str, Any], source_file: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    part, number = spec["part"], spec["chapter"]
    key = chapter_key(part, number)
    title_event = next((event for event in events if event["k"] == "chapter"), None)
    if title_event is None:
        raise TexParseError("缺少 \\chapter 标题")
    chapter = {
        "key": key, "part": part, "part_title": PART_TITLES[part], "chapter": number,
        "title": title_event["title"], "source_file": source_file, "line": title_event["line"],
    }
    issues: list[str] = []
    warnings: list[str] = []
    # Split the chapter into its intro, its numbered sections and its exercise regions.
    units: list[dict[str, Any]] = [{"title": "", "events": [], "line": title_event["line"], "intro": True}]
    regions: list[dict[str, Any]] = []
    body_blocks: list[tuple[dict[str, Any], str]] = []  # (block, section key) in book order
    for event in events[events.index(title_event) + 1:]:
        if event["k"] == "section" and event["numbered"]:
            units.append({"title": event["title"], "full_title": event["full_title"], "events": [],
                          "line": event["line"], "intro": False})
            continue
        if event["k"] in {"exercise_region", "answers_region"}:
            if not regions:
                # One card for all of the chapter's exercises, where the first one starts.
                units[-1]["events"].append({"k": "exercise_link"})
            regions.append({"kind": event["k"], "events": event["events"], "line": event["line"]})
            continue
        units[-1]["events"].append(event)
    sections: list[dict[str, Any]] = []
    numbered = 0
    for unit in units:
        if unit["intro"]:
            if not any(event["k"] != "exercise_link" for event in unit["events"]) and len(units) > 1:
                continue
            section_key = f"{key}-00"
            title = "导言" if len(units) > 1 else chapter["title"]
        else:
            numbered += 1
            section_key = f"{key}-{numbered:02d}"
            title = unit["title"]
        blocks: list[dict[str, Any]] = []
        content = [event for event in unit["events"] if event["k"] != "exercise_link"]
        for event in unit["events"]:
            if event["k"] == "exercise_link":
                blocks.append({"kind": "exercise_link", "chapter_key": key, "count": 0})
                continue
            try:
                for block in to_blocks([event]):
                    blocks.append(block)
                    body_blocks.append((block, section_key))
            except TexParseError as error:
                issues.append(f"（{title}）{error}")
        sections.append({
            "key": section_key, "chapter_key": key, "part": part, "chapter": number, "title": title,
            "full_title": unit.get("full_title") or title, "intro": unit["intro"], "numbered": not unit["intro"],
            "blocks": blocks, "source_file": source_file, "source_line": unit["line"],
            "empty": not content,
        })
    built = _assemble_exercises(chapter, regions, body_blocks)
    for section in sections:
        for block in section["blocks"]:
            if block["kind"] == "exercise_link":
                block["count"] = len(built["questions"])
    return {"chapter": {**chapter, "question_count": len(built["questions"]),
                        "section_keys": [section["key"] for section in sections]},
            "sections": sections, "groups": built["groups"], "questions": built["questions"],
            "issues": issues + built["issues"], "warnings": warnings + built["warnings"]}


# -- exercises ------------------------------------------------------------------

def _group_type(english: str, region_title: str) -> tuple[str, str] | None:
    """(question type, answer size) from a group's English instruction."""

    text = english.casefold().strip()
    if "标点" in region_title:
        return "punctuation", "sentence"
    if text.startswith(("fill in the blanks", "put the words in parentheses")):
        return "fill_blank", "sentence"
    if text.startswith(("correct all the mistakes", "correct the mistakes")):
        return "correction", "sentence"
    if text.startswith("combine"):
        return "combine", "sentence"
    if text.startswith(("simplify", "rewrite", "correct any vagueness")) or "同义表达" in region_title:
        return "rewrite", "sentence"
    if text.startswith("write a thesis statement"):
        return "writing", "sentence"
    return None


def _assemble_exercises(chapter: dict[str, Any], regions: list[dict[str, Any]],
                        body_blocks: list[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    key = chapter["key"]
    issues: list[str] = []
    warnings: list[str] = []
    groups: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    prompt_count = 0
    position = 0
    while position < len(regions):
        region = regions[position]
        if region["kind"] != "exercise_region":
            issues.append(f"第 {region['line']} 行：参考答案前没有对应的写作练习")
            position += 1
            continue
        answers = regions[position + 1] if position + 1 < len(regions) else None
        if answers is None or answers["kind"] != "answers_region":
            issues.append(f"第 {region['line']} 行：写作练习后没有参考答案")
            answers = {"events": [], "line": region["line"]}
            position += 1
        else:
            position += 2
        region_groups = _exercise_groups(region, issues)
        segments = _answer_segments(answers["events"])
        for group in region_groups:
            letter = group["letter"]
            if group["prompt"] is not None:
                prompt_count += 1
                code = f"{key}-P{prompt_count}"
                group["prompt_code"] = code
            group_code = f"{key}-{letter}" if letter else group["prompt_code"]
            segment = _match_segment(group, segments)
            if segment is None:
                issues.append(f"{group_code}：找不到对应的参考答案")
                continue
            source_page_line = group["line"]
            group_row = {
                "group_code": group_code, "chapter_key": key, "part": chapter["part"],
                "part_title": chapter["part_title"], "chapter": chapter["chapter"],
                "chapter_title": chapter["title"], "letter": letter, "region_title": group["region_title"],
                "title": group["title"], "instruction": group["en"], "instruction_zh": group["zh"],
                "intro": group["intro"], "notes": group["notes"], "question_type": group["type"],
                "answer_size": group["size"], "source_file": chapter["source_file"], "line": source_page_line,
                "context": None,
            }
            built: list[dict[str, Any]] = []
            if group["prompt"] is not None:
                stem = blocks_to_runs(group["prompt"])
                answer = _prompt_answer(segment["loose"])
                context = _prompt_context(stem, body_blocks)
                group_row["context"] = context
                built.append({
                    "question_code": group["prompt_code"], "number": prompt_count, "number_label": "写作",
                    "stem": stem, **answer, "answer_key": None, "line": group["prompt_line"], "context": context,
                })
                if not answer["model_answer"]:
                    issues.append(f"{group['prompt_code']}：参考范文为空")
            else:
                entries = {entry["number"]: entry for entry in segment["items"]}
                if [entry["number"] for entry in segment["items"]] != [item["number"] for item in group["items"]]:
                    issues.append(
                        f"{group_code}：题号 {[item['number'] for item in group['items']]} 与答案题号 "
                        f"{[entry['number'] for entry in segment['items']]} 对不上"
                    )
                for item in group["items"]:
                    entry = entries.get(item["number"])
                    if entry is None:
                        continue
                    answer = _item_answer(entry)
                    code = f"{group_code}-{int(item['number']):02d}"
                    built.append({
                        "question_code": code, "number": int(item["number"]), "number_label": item["number"],
                        "stem": item["runs"], **answer, "answer_key": None, "line": item["line"], "context": None,
                    })
                    if any(run.get("line") for run in item["runs"]) and group["type"] != "combine":
                        warnings.append(f"{code}：题干中有一个印刷换行（\\\\），按原样显示为换行")
            for question in built:
                question.update({
                    "group_code": group_code, "chapter_key": key, "part": chapter["part"],
                    "part_title": chapter["part_title"], "chapter": chapter["chapter"],
                    "chapter_title": chapter["title"], "letter": letter, "group_title": group["title"],
                    "question_type": group["type"], "type_name": TYPE_NAMES[group["type"]],
                    "answer_size": group["size"], "instruction": group["en"], "instruction_zh": group["zh"],
                    "reference_label": "原书范文" if group["type"] == "writing" and group["size"] != "sentence"
                    else "原书答案",
                    "source_file": chapter["source_file"], "warnings": [],
                })
                if group["type"] == "fill_blank":
                    question["answer_key"] = _blank_key(question, issues)
                if not question["model_answer"]:
                    issues.append(f"{question['question_code']}：答案为空")
                if not question["stem"]:
                    issues.append(f"{question['question_code']}：题干为空")
            group_row["question_codes"] = [question["question_code"] for question in built]
            groups.append(group_row)
            questions.extend(built)
    return {"groups": groups, "questions": questions, "issues": issues, "warnings": warnings}


def _exercise_groups(region: dict[str, Any], issues: list[str]) -> list[dict[str, Any]]:
    """The groups of one ``writingexercise``: a heading/instruction, notes, items or a prompt."""

    region_title = ""
    intro: list[list[dict[str, Any]]] = []
    groups: list[dict[str, Any]] = []
    subsections = 0

    def new_group(letter: str, en: list, zh: list, title: str, line: int) -> dict[str, Any]:
        group = {"letter": letter, "en": en, "zh": zh, "title": title, "notes": [], "intro": list(intro),
                 "items": [], "prompt": None, "prompt_line": 0, "line": line, "region_title": region_title}
        groups.append(group)
        return group

    for event in region["events"]:
        kind = event["k"]
        current = groups[-1] if groups else None
        if kind == "section" and not event["numbered"]:
            region_title = event["title"]
        elif kind == "para":
            if current is None:
                intro.append(event["runs"])
            elif current["items"] or current["prompt"] is not None:
                issues.append(f"第 {event['line']} 行：练习题之间出现了说明文字")
            else:
                current["notes"].append(event["runs"])
        elif kind == "exercise_heading":
            new_group(event["letter"], event["en"], event["zh"], f"{event['letter']} 组", event["line"])
        elif kind == "exercise_instruction":
            new_group("E", event["en"], event["zh"], "", event["line"])
        elif kind == "subsection":
            subsections += 1
            new_group(f"S{subsections}", [], [], event["title"], event["line"])
        elif kind == "exercise_item":
            if current is None or current["prompt"] is not None:
                current = _implicit_group(new_group, intro, event["line"])
            current["items"].append({"number": event["number"], "runs": event["runs"], "line": event["line"]})
        elif kind == "prompt":
            if current is None or current["items"] or current["prompt"] is not None:
                current = new_group("", [], [], region_title, event["line"])
                current["intro"] = list(intro)
            current["prompt"] = to_blocks(event["events"])
            current["prompt_line"] = event["line"]
        else:
            issues.append(f"第 {event['line']} 行：写作练习中出现了无法归属的内容：{kind}")
    for group in groups:
        if not group["en"] and group["letter"].startswith("S"):
            # 同义表达: the shared instruction is the region's own text.
            group["intro"] = list(intro)
        if group["prompt"] is not None:
            heading = runs_to_text(group["en"]).casefold()
            prompt_text = runs_to_text(blocks_to_runs(group["prompt"])).casefold()
            size = "paragraph" if "first paragraph" in heading or "summary" in prompt_text else "essay"
            group["type"], group["size"] = "writing", size
            continue
        detected = _group_type(runs_to_text(group["en"]), group["region_title"])
        if detected is None:
            issues.append(f"第 {group['line']} 行：无法判断练习题型（要求：{runs_to_text(group['en']) or '无'}）")
            detected = ("rewrite", "sentence")
        group["type"], group["size"] = detected
    return groups


def _implicit_group(new_group: Any, intro: list[list[dict[str, Any]]], line: int) -> dict[str, Any]:
    """Items without a heading: the instruction is a bold English line (and the Chinese one
    after it) in the region's intro, or only the intro text."""

    en: list[dict[str, Any]] = []
    zh: list[dict[str, Any]] = []
    for position, paragraph in enumerate(intro):
        if paragraph and all(run.get("b") for run in paragraph if "t" in run) and not is_chinese(paragraph):
            en = [{key: value for key, value in run.items() if key != "b"} for run in paragraph]
            following = intro[position + 1] if position + 1 < len(intro) else []
            zh = following if following and is_chinese(following) else []
            del intro[position:position + (2 if zh else 1)]
            break
    return new_group("E", en, zh, "", line)


def _answer_segments(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The answer region split at its A/B/C headings; each segment holds its numbered
    answers (with the blocks after each) or, for an essay prompt, loose blocks."""

    segments: list[dict[str, Any]] = [{"letter": "", "title": "", "items": [], "loose": []}]
    for event in events:
        kind = event["k"]
        if kind == "section" and not event["numbered"]:
            continue
        if kind == "answer_heading":
            segments.append({"letter": event["letter"], "title": "", "items": [], "loose": []})
        elif kind == "subsection":
            title = event["title"]
            segments.append({"letter": title if re.fullmatch(r"[A-Z]", title) else "", "title": title,
                             "items": [], "loose": []})
        elif kind == "answer_item":
            segments[-1]["items"].append({"number": event["number"], "blocks": event["blocks"], "after": [],
                                          "line": event["line"]})
        elif segments[-1]["items"]:
            segments[-1]["items"][-1]["after"].append(event)
        else:
            segments[-1]["loose"].append(event)
    return [segment for segment in segments if segment["items"] or segment["loose"]]


def _match_segment(group: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    letter = group["letter"]
    if letter and len(letter) == 1 and letter != "E":
        return next((segment for segment in segments if segment["letter"] == letter), None)
    if letter.startswith("S"):
        return next((segment for segment in segments if segment["title"] == group["title"]), None)
    return segments[0] if len(segments) == 1 else None


def _item_answer(entry: dict[str, Any]) -> dict[str, Any]:
    """Split one numbered answer into the English answer(s), the Chinese and the explanation."""

    answer: list[list[dict[str, Any]]] = []
    chinese: list[list[dict[str, Any]]] = []
    explanation: list[dict[str, Any]] = []
    for event in [*entry["blocks"], *entry["after"]]:
        if event["k"] == "para":
            for line in split_lines(event["runs"]):
                (chinese if is_chinese(line) else answer).append(line)
        elif event["k"] == "translation":
            chinese.append(event["runs"])
        elif event["k"] == "solution":
            explanation.extend(to_blocks(event["events"]))
        else:
            explanation.extend(to_blocks([event]))
    return {"model_answer": _join(answer), "model_answer_zh": _join(chinese),
            "explanation": blocks_to_runs(explanation)}


def _prompt_answer(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The model essay of a prompt: the text before 翻译, the translation, the 说明."""

    model: list[dict[str, Any]] = []
    chinese: list[dict[str, Any]] = []
    explanation: list[dict[str, Any]] = []
    target = None
    for event in events:
        if event["k"] == "label":
            target = {"翻译": chinese, "说明": explanation}.get(event["text"])
            if target is None:
                explanation.extend(to_blocks([event]))
            continue
        if event["k"] == "solution":
            explanation.extend(to_blocks(event["events"]))
            continue
        blocks = to_blocks([event])
        if target is not None:
            target.extend(blocks)
        elif event["k"] == "para" and is_chinese(event["runs"]):
            chinese.extend(blocks)
        else:
            model.extend(blocks)
    return {"model_answer": blocks_to_runs(model), "model_answer_zh": blocks_to_runs(chinese),
            "explanation": blocks_to_runs(explanation)}


def _prompt_context(stem: list[dict[str, Any]], body_blocks: list[tuple[dict[str, Any], str]]) -> dict[str, Any] | None:
    """Lecture material the task refers to: the outline "above" or the essay to summarise."""

    text = runs_to_text(stem)
    if "outline above" in text.casefold():
        framed = [position for position, (block, _key) in enumerate(body_blocks)
                  if block["kind"] == "outline" and block["framed"]]
        if not framed:
            return None
        start = framed[-1]
        chosen = [body_blocks[start]]
        for block, key in body_blocks[start + 1:]:
            if block["kind"] != "outline":
                break
            chosen.append((block, key))
        return {"title": "原书大纲", "section_key": chosen[0][1], "blocks": [block for block, _key in chosen]}
    match = re.search(r"summary on “([^”]+?),?”", text)
    if match:
        wanted = _squash(match.group(1))
        for block, key in reversed(body_blocks):
            if block["kind"] == "essay" and wanted in _squash(runs_to_text(blocks_to_runs([block]))):
                return {"title": "原书范文", "section_key": key, "blocks": [block]}
    return None


def _join(parts: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for part in parts:
        if runs:
            runs.append({"line": True})
        runs.extend(part)
    return normalize_runs(runs)


def _blank_key(question: dict[str, Any], issues: list[str]) -> dict[str, Any] | None:
    """Accepted words per blank: "a / b" is one answer per blank when the counts match,
    otherwise the alternatives of a single blank."""

    blanks = sum(1 for run in question["stem"] if run.get("blank"))
    first = split_lines(question["model_answer"])
    text = runs_to_text(first[0]) if first else ""
    parts = [part.strip() for part in text.split(" / ") if part.strip()]
    if not blanks or not parts:
        issues.append(f"{question['question_code']}：填空题缺少空格或答案")
        return None
    if len(parts) == blanks:
        return {"kind": "blanks", "blanks": [[part] for part in parts]}
    if blanks == 1:
        return {"kind": "blanks", "blanks": [parts]}
    issues.append(f"{question['question_code']}：{blanks} 个空格与答案「{text}」对不上")
    return None


def normalize_answer(value: str) -> str:
    """A blank's answer for comparison: case, spaces and quote shapes ignored."""

    text = unicodedata.normalize("NFKC", str(value or "")).replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().casefold()


def check_blanks(answer_key: dict[str, Any] | None, responses: list[str]) -> bool:
    blanks = (answer_key or {}).get("blanks") or []
    if len(responses) != len(blanks):
        return False
    return all(normalize_answer(response) in {normalize_answer(item) for item in accepted}
               for response, accepted in zip(responses, blanks))


# ---------------------------------------------------------------------------
# Residues and pages

_RESIDUE = re.compile(r"\\|[{}]|Writing[A-Z]|linewidth|\[(?:-?[\d.]+\s*(?:em|ex|pt|cm|mm)|[lcr])\]")


def text_residues(value: Any) -> list[str]:
    """TeX left in any text of a block, run list or question (empty when clean)."""

    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"t", "text", "title", "full_title", "label"} and isinstance(child, str):
                    found.extend(match.group(0) for match in _RESIDUE.finditer(child))
                elif isinstance(child, (list, dict)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return sorted(set(found))


def _normal_title(title: str) -> str:
    return re.sub(r"[\s:：]+", "", unicodedata.normalize("NFKC", str(title or "")))


def locate(book: dict[str, Any], pdf_path: Path | str) -> dict[str, int]:
    """PDF pages: sections from the bookmarks, questions by their text within the chapter."""

    import fitz

    with fitz.open(str(pdf_path)) as document:
        toc = document.get_toc()
        pages = [_squash(page.get_text()) for page in document]
    chapter_pages: list[tuple[int, list[tuple[str, int]]]] = []
    for level, title, page in toc:
        if level == 2:
            chapter_pages.append((page, []))
        elif level == 3 and chapter_pages:
            chapter_pages[-1][1].append((title, page))
    counts = {"sections": 0, "section_mismatch": 0, "located": 0, "inferred": 0}
    starts: dict[str, int] = {}
    for chapter, (page, bookmarks) in zip(book["chapters"], chapter_pages):
        starts[chapter["key"]] = page
        chapter["source_page"] = page
        numbered = [section for section in book["sections"]
                    if section["chapter_key"] == chapter["key"] and section["numbered"]]
        for section in book["sections"]:
            if section["chapter_key"] == chapter["key"] and not section["numbered"]:
                section["source_page"], section["page_status"] = page, "bookmark"
        if len(numbered) != len(bookmarks):
            counts["section_mismatch"] += 1
        for section, (title, bookmark_page) in zip(numbered, bookmarks):
            section["source_page"] = bookmark_page
            same = _normal_title(title) == _normal_title(section["title"])
            section["page_status"] = "bookmark" if same else "bookmark_title_differs"
            counts["sections"] += 1
            if not same:
                counts["section_mismatch"] += 1
    ends = {chapter["key"]: (chapter_pages[index + 1][0] if index + 1 < len(chapter_pages) else len(pages) + 1)
            for index, chapter in enumerate(book["chapters"])}
    for group in book["groups"]:
        start = starts.get(group["chapter_key"], 1)
        end = ends.get(group["chapter_key"], len(pages) + 1)
        group_page = None
        for item in [question for question in book["questions"] if question["group_code"] == group["group_code"]]:
            stem = _squash(runs_to_text(item["stem"]))
            keys = [key for key in (stem[:36], stem[8:44]) if len(key) >= 12]
            found = _search(pages, keys, max(0, (group_page or start) - 1), window=max(1, end - (group_page or start)))
            if found is not None:
                item["source_page"], item["page_status"] = found + 1, "located"
                group_page = group_page or found + 1
                counts["located"] += 1
            else:
                item["source_page"], item["page_status"] = None, "inferred"
        group["source_page"] = group_page or start
        for item in book["questions"]:
            if item["group_code"] == group["group_code"] and item["source_page"] is None:
                item["source_page"] = group["source_page"]
                counts["inferred"] += 1
    return counts


__all__ = [
    "BOOK_TITLE",
    "PART_NUMERALS",
    "PART_TITLES",
    "PDF_NAME",
    "QUESTION_CODE_PREFIX",
    "TYPE_NAMES",
    "TexParseError",
    "blocks_to_runs",
    "chapter_key",
    "chapter_order",
    "check_blanks",
    "is_chinese",
    "locate",
    "normalize_answer",
    "parse_book",
    "parse_inline",
    "runs_to_text",
    "scan_blocks",
    "text_residues",
]
