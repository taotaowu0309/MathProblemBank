"""Structured parser for the TeX source of 《旋元佑文法》.

The book has three parts (单句, 复合句, 减化子句) and an appendix (广读学英文).
A chapter is a lecture made of ``\\section``\\ s: running text with key terms
(``\\rdterm``), example boxes (``example`` / ``rdsafeexample`` /
``rdbreakexample``) holding example sentences — plain (``\\rdsimpleexample``),
marked wrong (``\\rderrorexample``) or analysed into labelled parts
(``rdsentence`` + ``rdgrammarline`` + ``\\rdseg``) — pattern formulas,
category pills, tables, numbered lists and the tense timelines of chapter 9.
Two chapters end with exercises and their solutions; they are lecture
material of the 文法 page like everything else (the bank's grammar questions
come from 《文法解题》).

The parser works like the reading and writing books' parsers:
:func:`scan_blocks` turns TeX into block events, layout-only commands are
dropped by an explicit list and any other unknown command is an error, so a
source change can never leak TeX into the program.

Rich text is stored as *runs* (as in the other books): ``{"t": text}`` plus
style keys ``b``, ``i``, ``u`` (underline), ``g`` (the book's coloured key
words), ``pat`` (a sentence pattern such as 〈S + V〉), ``pill`` (a category
label such as 限定词) and ``frame`` (a boxed word); the special runs
``{"br": True}`` and ``{"line": True}``.  A part of an analysed sentence is
a run (or several neighbouring runs) carrying ``seg`` — the label the book
prints under it, such as ``S`` or ``从属子句`` — and ``sid``, which numbers the
parts within the run list so that two neighbouring parts never merge; ``u``
tells whether the book underlines the part (``\\rdplainseg`` does not).
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
    _skip_spaces,
    normalize_runs,
    runs_to_text,
    strip_comments,
)
from shared.scripts.raydu_writing_parser import is_chinese, split_paragraphs

KEY_PREFIX = "XYGM"
BOOK_TITLE = "旋元佑文法"
MAIN_TEX = "raydu-elements-v2-font-final.tex"
PDF_NAME = "raydu-elements-v2-font-final.pdf"
PART_NUMERALS = {1: "第一部分", 2: "第二部分", 3: "第三部分", 4: "第四部分"}

# The gap after an analysed part: the book's default is a word space; an
# optional ``[0pt]`` joins the part to what follows (punctuation, a bracket).
_DEFAULT_SEGMENT_GAP = True


# ---------------------------------------------------------------------------
# Inline rich text

_IGNORED_INLINE = {
    "noindent", "leavevmode", "normalcolor", "strut", "relax", "raggedright", "centering", "sffamily",
    "normalsize", "small", "scriptsize", "large", "Large", "rdBodyReset", "begingroup", "endgroup", "allowbreak", "nobreak",
    "medskip", "smallskip", "bigskip",
}
_SYMBOLS = {
    "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒", "times": "×", "ldots": "…", "dots": "…",
    "textellipsis": "…", "textbullet": "•", "textquoteright": "’", "textquoteleft": "‘", "textquotedblleft": "“",
    "textquotedblright": "”", "textemdash": "—", "textendash": "–",
}
_SPACES = {"quad": "\u2003", "qquad": "\u2003\u2003", "enspace": " ", "thinspace": " ", "space": " ", "hfill": " "}
_STYLES = {
    "textbf": {"b": True}, "textit": {"i": True}, "emph": {"i": True}, "uline": {"u": True},
    "underline": {"u": True}, "rdkey": {"g": True, "b": True}, "rdcolor": {"g": True}, "rdpill": {"pill": True},
    "fbox": {"frame": True}, "textsuperscript": {"sup": True}, "mbox": {}, "text": {}, "textrm": {},
    "textnormal": {},
}
_ESCAPED = {"%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", " ": " ", ",": "\u2009"}
# Layout commands dropped with their arguments: name -> number of arguments.
_DROP_ARGS = {"vspace": 1, "hspace": 1, "color": 1, "label": 1, "setlength": 2, "addvspace": 1}


def _gap(optional: str | None, default: bool) -> bool:
    """Whether an ``\\rdseg[...]`` leaves a word space after the part."""

    if optional is None:
        return default
    match = re.fullmatch(r"\s*(-?[\d.]+)\s*(pt|em|ex|mm|cm)?\s*", optional)
    if match is None:
        raise TexParseError(f"无法识别的成分间距：[{optional}]")
    return float(match.group(1)) > 0


def parse_inline(source: str, *, segment_gap: bool = _DEFAULT_SEGMENT_GAP) -> list[dict[str, Any]]:
    """Convert inline TeX of the grammar book into style runs."""

    runs: list[dict[str, Any]] = []
    segments = [0]

    def walk(text: str, style: dict[str, Any], *, math: bool = False) -> None:
        index = 0
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                runs.append({"t": "".join(buffer), **style})
                buffer.clear()

        while index < len(text):
            character = text[index]
            if character == "\\":
                match = re.match(r"\\([A-Za-z@]+)(\*?)", text[index:])
                if match is None:
                    following = text[index + 1:index + 2]
                    if following == "\\":
                        flush()
                        runs.append({"line": True})
                        index += 2
                        if text[_skip_spaces(text, index):].startswith("["):
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
                if name == "bfseries":
                    # A switch: bold to the end of the enclosing group.
                    flush()
                    style = {**style, "b": True}
                    continue
                if name == "newline":
                    flush()
                    runs.append({"line": True})
                    continue
                if name == "unskip":
                    # \rdraw{\unskip, and }: the comma follows the part directly.
                    flush()
                    while runs and "t" in runs[-1]:
                        runs[-1]["t"] = runs[-1]["t"].rstrip()
                        if runs[-1]["t"]:
                            break
                        runs.pop()
                    continue
                if name in _DROP_ARGS:
                    _values, index = _read_args(text, index, _DROP_ARGS[name])
                    continue
                if name in {"rdseg", "rdplainseg"}:
                    optional, cursor = _read_optional(text, index)
                    (content, label), index = _read_args(text, cursor, 2)
                    flush()
                    segments[0] += 1
                    part_style = {**style, "seg": _plain(label), "sid": segments[0]}
                    if name == "rdseg":
                        part_style["u"] = True
                    walk(content, part_style)
                    if _gap(optional, segment_gap):
                        buffer.append(" ")
                    continue
                if name == "rdraw":
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(content, style)
                    continue
                if name == "rdpattern":
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(f"〈{content}〉", {**style, "pat": True})
                    continue
                if name in _STYLES:
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(content, {**style, **_STYLES[name]})
                    continue
                raise TexParseError(f"无法识别的命令：\\{name}")
            if character == "{":
                content, index = _read_group(text, index)
                flush()
                walk(content, dict(style), math=math)
                continue
            if character == "}":
                raise TexParseError("多余的右花括号")
            if character == "$":
                if math:
                    raise TexParseError("数学模式嵌套")
                end = text.find("$", index + 1)
                if end < 0:
                    raise TexParseError("数学模式 $ 不成对")
                flush()
                walk(text[index + 1:end], dict(style), math=True)
                index = end + 1
                continue
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
            elif math and character.isspace():
                pass
            else:
                buffer.append(character)
            index += 1
        flush()

    walk(str(source or ""), {})
    return _single_spaces(normalize_runs(runs))


def _single_spaces(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One space between words: :func:`normalize_runs` collapses spaces within
    each source run but not where it merges two of them, and an analysed part
    followed by a source line end or ``\\ `` leaves two."""

    tidy: list[dict[str, Any]] = []
    previous_space = True
    for run in runs:
        if "t" not in run:
            tidy.append(run)
            previous_space = bool(run.get("br") or run.get("line"))
            continue
        text = re.sub(r" {2,}", " ", run["t"])
        if previous_space:
            text = text.lstrip(" ")
        if text:
            tidy.append({**run, "t": text})
            previous_space = text.endswith(" ")
    return tidy


def _plain(source: str) -> str:
    return runs_to_text(parse_inline(source)).strip()


# ---------------------------------------------------------------------------
# Block events

# Block macros: name -> (number of mandatory arguments, event kind).
_BLOCK_MACROS = {
    "chapter": (1, "chapter"),
    "section": (1, "section"),
    "rdterm": (2, "term"),
    "rdtranslation": (1, "translation"),
    "rdsimpleexample": (2, "simple"),
    "rderrorexample": (2, "error"),
    "item": (0, "item"),
    "setcounter": (2, "setcounter"),
}
# Commands dropped at block level with their arguments.
_DROP_BLOCK = {"clearpage": 0, "thispagestyle": 1, "definecolor": 3, "tikzset": 1, "addcontentsline": 3,
               "markboth": 2}
_TITLED_BOXES = {"example": "example", "rdsafeexample": "example", "rdbreakexample": "example", "Exercise": "exercise"}
_NESTED_ENVS = {"rdanswerblock": "answer", "quote": "quote", "center": "center"}
_LIST_ENVS = {"rdnumberlist": True, "rdbulletlist": False}
_TABLE_ENVS = {"rdtable", "rdheadtable", "tabular"}
# The tense timelines of chapter 9: helper name -> number of arguments.
_TIMELINE_COMMANDS = {
    "TLbase": 0, "TLnow": 1, "TLxmark": 1, "TLparen": 2, "TLverb": 2, "TLverbhi": 2, "TLbelow": 2, "TLbox": 2,
    "TLperfect": 3,
}
_TIMELINE_DEFINITIONS = {"TLsetup", "TLend", *_TIMELINE_COMMANDS}
_TIMELINE_WIDTH = 10.2  # \TLbase draws the arrow from 0 to 10.2


def _contains_block(text: str) -> bool:
    names = sorted([*_BLOCK_MACROS, *_DROP_BLOCK, "TLsetup", "input"], key=len, reverse=True)
    return bool(re.search(r"\\(?:begin\{|(?:" + "|".join(names) + r")\b)", text))


class _Lines:
    def __init__(self, text: str, offset: int) -> None:
        self.starts = [0] + [match.end() for match in re.finditer("\n", text)]
        self.offset = offset

    def __call__(self, position: int) -> int:
        return self.offset + bisect_right(self.starts, position)


class _Context:
    """State that a chapter's source can change: where included files live and
    whether its grammar lines are compact (chapter 24 redefines ``\\rdseg`` so
    that parts join without a space and source line ends add none)."""

    def __init__(self, chapters_dir: Path | None = None) -> None:
        self.chapters_dir = chapters_dir
        self.compact = False


def scan_blocks(text: str, context: _Context | None = None, *, line_offset: int = 0) -> list[dict[str, Any]]:
    """Turn TeX into a list of block events (paragraphs, boxes, sentences, lists...)."""

    context = context or _Context()
    events: list[dict[str, Any]] = []
    buffer: list[str] = []
    buffer_start = [0]
    line_of = _Lines(text, line_offset)
    index = 0

    def located(error: TexParseError, position: int) -> TexParseError:
        message = str(error)
        return error if message.startswith("第 ") else TexParseError(f"第 {line_of(position)} 行：{message}")

    def inline(source: str, position: int) -> list[dict[str, Any]]:
        try:
            return parse_inline(source, segment_gap=not context.compact)
        except TexParseError as error:
            raise located(error, position) from None

    def flush() -> None:
        raw = "".join(buffer)
        buffer.clear()
        if not raw.strip():
            return
        for paragraph in split_paragraphs(inline(raw, buffer_start[0])):
            events.append({"k": "para", "runs": paragraph, "line": line_of(buffer_start[0])})

    def append(chunk: str, position: int) -> None:
        if not buffer or not "".join(buffer).strip():
            buffer_start[0] = position
        buffer.append(chunk)

    while index < len(text):
        character = text[index]
        if character == "\n" and re.match(r"\n[ \t]*\n", text[index:]):
            flush()
            index = _skip_spaces(text, index)
            continue
        if character == "{":
            content, end = _read_group(text, index)
            if _contains_block(content):
                flush()
                events.extend(scan_blocks(content, context, line_offset=line_of(index) - 1))
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
        name = match.group(1)
        after = index + match.end()
        position = index
        try:
            if name == "begin":
                (environment,), body_start = _read_args(text, after, 1)
                environment = environment.strip()
                end_start, end_after = _find_environment_end(text, environment, body_start)
                flush()
                found = _environment_events(environment, text[body_start:end_start], line_of(position), context)
                events.extend(found)
                index = end_after
                continue
            if name == "TLsetup":
                end = text.find("\\TLend", after)
                if end < 0:
                    raise TexParseError("时间轴缺少 \\TLend")
                flush()
                events.append({"k": "timeline", "items": _timeline(text[after:end]), "line": line_of(position)})
                index = end + len("\\TLend")
                continue
            if name in {"newcommand", "renewcommand"}:
                index = _skip_definition(text, after, star=bool(match.group(2)))
                continue
            if name == "newenvironment":
                (defined,), cursor = _read_args(text, after, 1)
                if defined.strip() != "rdanswerblock":
                    raise TexParseError(f"章节中定义了未知环境 {defined}")
                _bodies, index = _read_args(text, cursor, 2)
                continue
            if name == "RenewDocumentCommand":
                (defined, _spec, _body), index = _read_args(text, after, 3)
                if defined.strip() != "\\rdseg":
                    raise TexParseError(f"章节中重定义了未知命令 {defined}")
                context.compact = True
                continue
            if name == "RenewDocumentEnvironment":
                (defined, _spec, _begin, _end), index = _read_args(text, after, 4)
                if defined.strip() != "rdgrammarline":
                    raise TexParseError(f"章节中重定义了未知环境 {defined}")
                context.compact = True
                continue
            if name in _DROP_BLOCK:
                _values, index = _read_args(text, after, _DROP_BLOCK[name])
                continue
            if name == "input":
                (target,), index = _read_args(text, after, 1)
                flush()
                events.extend(_included(target, context))
                continue
            if name in _BLOCK_MACROS:
                count, kind = _BLOCK_MACROS[name]
                short, cursor = _read_optional(text, after) if kind in {"chapter", "section"} else (None, after)
                args, index = _read_args(text, cursor, count) if count else ([], cursor)
                flush()
                event: dict[str, Any] = {"k": kind, "line": line_of(position)}
                if kind in {"chapter", "section"}:
                    # \section[short]{\rdkey{long}}: the short title is the one of the bookmarks.
                    event["title"] = runs_to_text(inline(short if short is not None else args[0], position))
                    event["numbered"] = not match.group(2)
                elif kind == "term":
                    event["runs"], event["note"] = inline(args[0], position), inline(args[1], position)
                elif kind == "simple":
                    event["en"], event["zh"] = inline(args[0], position), inline(args[1], position)
                elif kind == "error":
                    event["en"], event["mark"] = inline(args[0], position), runs_to_text(inline(args[1], position))
                    event["zh"] = []
                    # The optional third argument (xparse ``g``): a group in the same paragraph.
                    cursor = index
                    while cursor < len(text) and text[cursor] in " \t":
                        cursor += 1
                    if text[cursor:cursor + 1] == "\n" and not re.match(r"\n[ \t]*\n", text[cursor:]):
                        cursor = _skip_spaces(text, cursor)
                    if text[cursor:cursor + 1] == "{":
                        third, index = _read_group(text, cursor)
                        event["zh"] = inline(third, position)
                elif kind == "setcounter":
                    if args[0].strip() != "enumi":
                        raise TexParseError(f"无法识别的计数器 {args[0]}")
                    event["value"] = int(args[1])
                elif kind == "translation":
                    event["runs"] = inline(args[0], position)
                events.append(event)
                continue
        except TexParseError as error:
            raise located(error, position) from None
        append(text[index:after], index)
        index = after
    flush()
    return events


def _skip_definition(text: str, index: int, *, star: bool) -> int:
    """Skip a ``\\newcommand`` of a known chapter helper (the timeline macros,
    the page-number mark of chapter 7); any other definition is an error."""

    (defined,), cursor = _read_args(text, index, 1)
    name = defined.strip().lstrip("\\")
    if name not in _TIMELINE_DEFINITIONS | {"outermarginpagenummark"}:
        raise TexParseError(f"章节中定义了未知命令 \\{name}")
    _count, cursor = _read_optional(text, cursor)
    _body, cursor = _read_args(text, cursor, 1)
    return cursor


def _included(target: str, context: _Context) -> list[dict[str, Any]]:
    relative = target.strip()
    if context.chapters_dir is None or not relative.startswith("chapters/"):
        raise TexParseError(f"无法定位 \\input{{{relative}}}")
    path = context.chapters_dir / Path(relative).name
    if path.suffix != ".tex":
        path = path.with_suffix(".tex")
    try:
        return scan_blocks(strip_comments(path.read_text(encoding="utf-8")), context)
    except TexParseError as error:
        raise TexParseError(f"{path.name} {error}") from None


def _environment_events(environment: str, body: str, line: int, context: _Context) -> list[dict[str, Any]]:
    def nested(source: str) -> list[dict[str, Any]]:
        return scan_blocks(source, context, line_offset=line - 1)

    def inline(source: str) -> list[dict[str, Any]]:
        return parse_inline(source, segment_gap=not context.compact)

    if environment in _TITLED_BOXES:
        title = None
        cursor = 0
        if environment in {"example", "Exercise"}:
            _options, cursor = _read_optional(body, 0)
        title, cursor = _read_optional(body, cursor)
        return [{"k": _TITLED_BOXES[environment], "title": _plain(title or ""), "events": nested(body[cursor:]),
                 "line": line}]
    if environment == "rdsentence":
        title, cursor = _read_optional(body, 0)
        return [{"k": "sentence", "title": _plain(title or ""), "events": nested(body[cursor:]), "line": line}]
    if environment == "rdgrammarline":
        # Some sources put the sentence's \rdtranslation inside its grammar line.
        translations: list[dict[str, Any]] = []
        for found in reversed(list(re.finditer(r"\\rdtranslation\b", body))):
            (content,), end = _read_args(body, found.end(), 1)
            translations.insert(0, {"k": "translation", "runs": inline(content), "line": line})
            body = body[:found.start()] + body[end:]
        if _contains_block(body):
            raise TexParseError(f"第 {line} 行：句法分析行中出现了块级内容")
        source = re.sub(r"\n[ \t]*", "", body) if context.compact else body
        runs = inline(source)
        return ([{"k": "grammarline", "runs": runs, "line": line}] if runs else []) + translations
    if environment == "rdpassage":
        return nested(body)
    if environment == "minipage":
        # The 前言's centred motto: only the width argument is layout.
        _position, cursor = _read_optional(body, 0)
        _width, cursor = _read_args(body, cursor, 1)
        return nested(body[cursor:])
    if environment in _NESTED_ENVS:
        return [{"k": _NESTED_ENVS[environment], "events": nested(body), "line": line}]
    if environment in _LIST_ENVS:
        return [{"k": "list", "ordered": _LIST_ENVS[environment], "events": nested(body), "line": line}]
    if environment in _TABLE_ENVS:
        return [_table(environment, body, line, inline)]
    if environment == "tikzpicture":
        return [{"k": "diagram", **_diagram(body, inline), "line": line}]
    raise TexParseError(f"第 {line} 行：无法识别的环境 {environment}")


# -- tables ---------------------------------------------------------------------

def _split_cells(text: str) -> list[str]:
    cells, depth, start = [], 0, 0
    for position, character in enumerate(text):
        escaped = text[position - 1:position] == "\\"
        if character == "{" and not escaped:
            depth += 1
        elif character == "}" and not escaped:
            depth -= 1
        elif character == "&" and depth == 0 and not escaped:
            cells.append(text[start:position])
            start = position + 1
    cells.append(text[start:])
    return cells


def _split_rows(text: str) -> list[str]:
    rows, depth, start, index = [], 0, 0, 0
    while index < len(text):
        character = text[index]
        if character == "\\" and text[index + 1:index + 2] == "\\" and depth == 0:
            rows.append(text[start:index])
            index += 2
            optional = re.match(r"\s*\[[^\]]*\]", text[index:])
            if optional:
                index += optional.end()
            start = index
            continue
        if character == "\\":
            index += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        index += 1
    rows.append(text[start:])
    return [row for row in rows if row.strip()]


def _column_spec(environment: str, spec: str) -> tuple[list[str], list[str]]:
    """Column alignments and the text set between neighbouring columns (the
    ``+`` of a noun-phrase formula), from a tabular or tabularray spec."""

    aligns: list[str] = []
    joins: list[str] = []
    pending = ""
    index = 0
    while index < len(spec):
        character = spec[index]
        if character.isspace() or character == "|":
            index += 1
            continue
        if environment == "tabular" and character == "@":
            content, index = _read_group(spec, index + 1)
            pending = _plain(content).replace("\u2003", " ").strip()
            continue
        if environment == "tabular" and character in "lcr":
            aligns.append(character)
            index += 1
        elif environment == "tabular" and character in "pmb":
            _width, index = _read_group(spec, index + 1)
            aligns.append("l")
        elif environment != "tabular" and character in "QX":
            options, index = _read_optional(spec, index + 1)
            found = re.search(r"(?:^|,)\s*([lcr])\s*(?:,|$)", options or "")
            aligns.append(found.group(1) if found else "l")
        else:
            raise TexParseError(f"无法识别的表格列格式：{spec}")
        if len(aligns) > 1:
            joins.append(pending)
        pending = ""
    return aligns, joins


def _table(environment: str, body: str, line: int, inline: Any) -> dict[str, Any]:
    (spec,), cursor = _read_args(body, 0, 1)
    aligns, joins = _column_spec(environment, spec)
    rows = []
    for raw_row in _split_rows(body[cursor:]):
        cells = [inline(cell) for cell in _split_cells(raw_row)]
        if len(cells) > len(aligns):
            raise TexParseError(f"第 {line} 行：表格行的单元格多于列数")
        rows.append(cells + [[] for _ in range(len(aligns) - len(cells))])
    return {"k": "table", "head": environment == "rdheadtable", "align": aligns, "joins": joins, "rows": rows,
            "line": line}


# -- figures --------------------------------------------------------------------

def _number(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError:
        raise TexParseError(f"无法识别的坐标：{text}") from None


def _timeline(body: str) -> list[dict[str, Any]]:
    """A ``\\TLsetup ... \\TLend`` tense timeline as drawing primitives in the
    book's coordinates (x from 0 to 10.2 along the axis, y up; one unit is
    0.92 cm): the axis arrow, crosses (now), labels (optionally boxed) and
    lines (optionally ending in an arrow).  The chapter's helper macros are
    expanded exactly as chapter 9 defines them; a figure may also use plain
    ``\\node`` and ``\\draw`` statements with the chapter's styles."""

    items: list[dict[str, Any]] = []

    def label(x: float, y: float, source: str, *, boxed: bool = False) -> None:
        items.append({"type": "label", "x": x, "y": y, "runs": parse_inline(source), "box": boxed})

    def line(points: list[tuple[float, float]], *, arrow: bool) -> None:
        items.append({"type": "line", "points": [[x, y] for x, y in points], "arrow": arrow})

    index = 0
    while True:
        index = _skip_spaces(body, index)
        if index >= len(body):
            break
        match = re.match(r"\\([A-Za-z]+)", body[index:])
        name = match.group(1) if match else ""
        if name in {"node", "draw"}:
            end = _statement_end(body, index)
            statement, index = body[index:end], end + 1
            styles, rest = _tikz_options(statement[match.end():])
            if name == "node":
                node = re.match(r"\s*at\s*\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)\s*", rest)
                if node is None or not styles <= {"rdbox", "rdlabel", "fill=white"}:
                    raise TexParseError(f"时间轴中出现了无法识别的节点：{statement[:50]!r}")
                content, after = _read_group(rest, node.end())
                if rest[after:].strip():
                    raise TexParseError(f"时间轴节点后有多余内容：{rest[after:]!r}")
                label(_number(node.group(1)), _number(node.group(2)), content, boxed="rdbox" in styles)
            else:
                points = [(float(x), float(y)) for x, y in re.findall(r"\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", rest)]
                shape = re.sub(r"\(\s*[-\d.]+\s*,\s*[-\d.]+\s*\)", "P", rest)
                shape = re.sub(r"\s+", "", shape)
                if len(styles) != 1 or not styles <= {"rdline", "rdvert", "rdarrow"} or shape != "--".join(
                        "P" * len(points)) or len(points) < 2:
                    raise TexParseError(f"时间轴中出现了无法识别的线段：{statement[:50]!r}")
                line(points, arrow=bool(styles & {"rdvert", "rdarrow"}))
            continue
        if name not in _TIMELINE_COMMANDS:
            raise TexParseError(f"时间轴中出现了无法识别的内容：{body[index:index + 30]!r}")
        args, index = _read_args(body, index + match.end(), _TIMELINE_COMMANDS[name])
        if name == "TLbase":
            line([(0.0, 0.0), (_TIMELINE_WIDTH, 0.0)], arrow=True)
        elif name in {"TLnow", "TLxmark"}:
            items.append({"type": "cross", "x": _number(args[0])})
            if name == "TLnow":
                label(_number(args[0]), -0.36, "now")
        elif name == "TLparen":
            label(_number(args[0]), 0.03, "(")
            label(_number(args[1]), 0.03, ")")
        elif name in {"TLverb", "TLverbhi", "TLbelow"}:
            label(_number(args[0]), {"TLverb": 0.47, "TLverbhi": 0.86, "TLbelow": -0.48}[name], args[1])
        elif name == "TLbox":
            point = re.fullmatch(r"\s*\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)\s*", args[0])
            if point is None:
                raise TexParseError(f"无法识别的时间轴方框位置：{args[0]}")
            label(_number(point.group(1)), _number(point.group(2)), args[1], boxed=True)
        else:
            start, end = _number(args[0]), _number(args[1])
            line([(start, 0.92), (end, 0.92)], arrow=False)
            line([(end, 0.92), (end, 0.02)], arrow=True)
            label(round(start + 0.60 * (end - start), 4), 0.69, args[2])
    if not items or items[0].get("type") != "line" or items[0]["points"] != [[0.0, 0.0], [_TIMELINE_WIDTH, 0.0]]:
        raise TexParseError("时间轴缺少开头的 \\TLbase")
    return items


def _statement_end(text: str, index: int) -> int:
    depth = 0
    for position in range(index, len(text)):
        character = text[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        elif character == ";" and depth == 0:
            return position
    raise TexParseError(f"图中的语句缺少分号：{text[index:index + 40]!r}")


def _tikz_options(text: str) -> tuple[set[str], str]:
    """The ``[...]`` options of a TikZ statement as a set, and the rest."""

    options, cursor = _read_optional(text, 0)
    return {option.strip() for option in (options or "").split(",") if option.strip()}, text[cursor:]


_COORDINATE = re.compile(r"(\+\+)?\(\s*(?:([A-Za-z]\w*)\.(\w+)|([-\d.]+)\s*,\s*([-\d.]+))\s*\)")


def _diagram(body: str, inline: Any) -> dict[str, Any]:
    """A small TikZ figure: labelled nodes and straight lines between node
    anchors (the nested noun phrase of chapter 2).  y grows upwards as in TikZ."""

    _options, cursor = _read_optional(body, 0)
    nodes: list[dict[str, Any]] = []
    paths: list[list[dict[str, Any]]] = []
    for statement in _split_statements(body[cursor:]):
        node = re.match(r"\\node\s*(?:\(\s*(\w+)\s*\))?\s*at\s*\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)\s*", statement)
        if node:
            content, end = _read_group(statement, node.end())
            if statement[end:].strip():
                raise TexParseError(f"图中节点后有多余内容：{statement[end:]!r}")
            nodes.append({"name": node.group(1) or "", "x": _number(node.group(2)), "y": _number(node.group(3)),
                          "runs": inline(content)})
            continue
        draw = re.match(r"\\draw\s*(?:\[[^\]]*\])?\s*", statement)
        if draw is None:
            raise TexParseError(f"图中出现了无法识别的语句：{statement[:40]!r}")
        points: list[dict[str, Any]] = []
        pen = False
        rest = statement[draw.end():]
        while rest.strip():
            rest = rest.lstrip()
            if rest.startswith("--"):
                pen, rest = True, rest[2:]
                continue
            coordinate = _COORDINATE.match(rest)
            if coordinate is None:
                raise TexParseError(f"图中出现了无法识别的路径：{rest[:40]!r}")
            relative, node_name, anchor, x, y = coordinate.groups()
            if relative:
                if not points:
                    raise TexParseError("路径不能以相对坐标开始")
                previous = points[-1]
                point = {**previous, "x": previous["x"] + _number(x or "0"), "y": previous["y"] + _number(y or "0")}
            elif node_name:
                point = {"node": node_name, "anchor": anchor, "x": 0.0, "y": 0.0}
            else:
                point = {"node": "", "anchor": "", "x": _number(x), "y": _number(y)}
            points.append({**point, "draw": pen and bool(points)})
            pen = False
            rest = rest[coordinate.end():]
        paths.append(points)
    names = {node["name"] for node in nodes if node["name"]}
    for points in paths:
        for point in points:
            if point["node"] and point["node"] not in names:
                raise TexParseError(f"图中的路径引用了不存在的节点 {point['node']}")
            if point["anchor"] not in {"", "west", "east", "north", "south", "center"}:
                raise TexParseError(f"图中出现了无法识别的锚点 {point['anchor']}")
    return {"nodes": nodes, "paths": paths}


def _split_statements(text: str) -> list[str]:
    statements, depth, start = [], 0, 0
    for position, character in enumerate(text):
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        elif character == ";" and depth == 0:
            statements.append(text[start:position].strip())
            start = position + 1
    if text[start:].strip():
        raise TexParseError(f"图中最后一条语句缺少分号：{text[start:].strip()[:40]!r}")
    return [statement for statement in statements if statement]


# ---------------------------------------------------------------------------
# Display blocks

def _sentence(title: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """An ``rdsentence``: its English lines (analysed or plain) and the
    translations and notes under them, in book order."""

    lines: list[dict[str, Any]] = []
    for event in events:
        if event["k"] == "grammarline":
            lines.append({"role": "en", "runs": event["runs"]})
        elif event["k"] == "translation":
            lines.append({"role": "zh", "runs": event["runs"]})
        elif event["k"] == "para":
            lines.append({"role": "zh" if is_chinese(event["runs"]) else "en", "runs": event["runs"]})
        else:
            raise TexParseError(f"第 {event.get('line', '?')} 行：例句中出现了无法归属的内容：{event['k']}")
    return {"kind": "sentence", "title": title, "error": "", "lines": lines}


def _list_block(event: dict[str, Any]) -> dict[str, Any]:
    items: list[list[dict[str, Any]]] = []
    start = 1
    for item in event["events"]:
        if item["k"] == "item":
            items.append([])
        elif item["k"] == "setcounter" and not items:
            start = item["value"] + 1
        elif items:
            items[-1].extend(to_blocks([item]))
        else:
            raise TexParseError(f"第 {item.get('line', '?')} 行：列表的第一项之前出现了内容")
    return {"kind": "list", "ordered": event["ordered"], "start": start, "items": items}


def to_blocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Events (without chapter and section headings) as display blocks."""

    blocks: list[dict[str, Any]] = []
    for event in events:
        kind = event["k"]
        if kind == "para":
            # An analysed sentence the source sets as a paragraph of its own.
            analysed = any("sid" in run for run in event["runs"])
            blocks.append({"kind": "analysis" if analysed else "para", "runs": event["runs"]})
        elif kind == "term":
            blocks.append({"kind": "term", "runs": event["runs"], "note": event["note"]})
        elif kind == "translation":
            blocks.append({"kind": "translation", "runs": event["runs"]})
        elif kind == "grammarline":
            blocks.append({"kind": "analysis", "runs": event["runs"]})
        elif kind == "simple":
            lines = [{"role": "en", "runs": event["en"]}]
            if event["zh"]:
                lines.append({"role": "zh", "runs": event["zh"]})
            blocks.append({"kind": "sentence", "title": "", "error": "", "lines": lines})
        elif kind == "error":
            lines = [{"role": "en", "runs": event["en"]}]
            if event["zh"]:
                lines.append({"role": "zh", "runs": event["zh"]})
            blocks.append({"kind": "sentence", "title": "", "error": event["mark"], "lines": lines})
        elif kind == "sentence":
            blocks.append(_sentence(event["title"], event["events"]))
        elif kind in {"example", "exercise"}:
            blocks.append({"kind": kind, "title": event["title"], "blocks": to_blocks(event["events"])})
        elif kind in {"answer", "quote"}:
            blocks.append({"kind": kind, "blocks": to_blocks(event["events"])})
        elif kind == "center":
            for block in to_blocks(event["events"]):
                if block["kind"] == "para":
                    block = {"kind": "centered", "runs": block["runs"]}
                elif block["kind"] in {"table", "diagram"}:
                    block = {**block, "centered": True}
                blocks.append(block)
        elif kind == "list":
            blocks.append(_list_block(event))
        elif kind == "table":
            blocks.append({"kind": "table", "head": event["head"], "align": event["align"], "joins": event["joins"],
                           "rows": event["rows"]})
        elif kind == "timeline":
            blocks.append({"kind": "timeline", "width": _TIMELINE_WIDTH, "items": event["items"]})
        elif kind == "diagram":
            blocks.append({"kind": "diagram", "nodes": event["nodes"], "paths": event["paths"]})
        else:
            raise TexParseError(f"第 {event.get('line', '?')} 行：讲解中出现了无法显示的内容：{kind}")
    return blocks


def blocks_text(blocks: Iterable[dict[str, Any]]) -> str:
    """Display blocks as plain text (search, notes and the AI tools)."""

    parts: list[str] = []
    for block in blocks:
        kind = block["kind"]
        if kind in {"para", "translation", "analysis", "centered"}:
            parts.append(runs_to_text(block["runs"]))
        elif kind == "term":
            parts.append(" ".join(filter(None, (runs_to_text(block["runs"]), runs_to_text(block["note"])))))
        elif kind == "sentence":
            if block["title"]:
                parts.append(block["title"])
            for line in block["lines"]:
                prefix = "* " if block["error"] and line is block["lines"][0] else ""
                parts.append(prefix + runs_to_text(line["runs"]))
            if block["error"]:
                parts.append(block["error"])
        elif kind in {"example", "exercise", "answer", "quote"}:
            if block.get("title"):
                parts.append(block["title"])
            parts.append(blocks_text(block["blocks"]))
        elif kind == "list":
            for number, item in enumerate(block["items"], start=block["start"]):
                parts.append((f"{number}. " if block["ordered"] else "• ") + blocks_text(item))
        elif kind == "table":
            for row in block["rows"]:
                cells = [runs_to_text(cell) for cell in row]
                line = cells[0] if cells else ""
                for join, cell in zip(block["joins"], cells[1:]):
                    line += f" {join} {cell}" if join else f"\t{cell}"
                parts.append(line.strip())
        elif kind == "timeline":
            parts.append(" ".join(runs_to_text(item["runs"]) for item in block["items"] if item.get("runs")))
        elif kind == "diagram":
            parts.append(" ".join(runs_to_text(node["runs"]) for node in block["nodes"]))
    return "\n".join(part for part in parts if part.strip())


def analysed_parts(runs: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """The labelled parts of an analysed line: ``[{"text": ..., "label": ...}]``."""

    parts: list[dict[str, Any]] = []
    previous = None
    for run in runs:
        if "sid" not in run:
            previous = None
            continue
        if run["sid"] == previous:
            parts[-1]["text"] += str(run.get("t") or "")
        else:
            parts.append({"text": str(run.get("t") or ""), "label": str(run.get("seg") or "")})
        previous = run["sid"]
    return [{"text": part["text"].strip(), "label": part["label"]} for part in parts]


def analysed_sentences(blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every analysed sentence of some blocks, in order, with its parts."""

    found: list[dict[str, Any]] = []

    def add(runs: list[dict[str, Any]]) -> None:
        parts = analysed_parts(runs)
        if parts:
            found.append({"sentence": runs_to_text(runs), "parts": parts})

    def walk(items: Iterable[dict[str, Any]]) -> None:
        for block in items:
            if block["kind"] == "sentence":
                for line in block["lines"]:
                    add(line["runs"])
            elif block["kind"] == "analysis":
                add(block["runs"])
            elif block["kind"] == "table":
                for row in block["rows"]:
                    for cell in row:
                        add(cell)
            walk(block.get("blocks", []))
            if block["kind"] == "list":
                for item in block["items"]:
                    walk(item)

    walk(blocks)
    return found


# ---------------------------------------------------------------------------
# The book: parts, chapters and sections

FRONT_TITLE = "导读"


def chapter_order(book_root: Path) -> list[dict[str, Any]]:
    """The chapter files in the order of the main TeX file, with their part.

    The front matter (总序, 前言) before ``\\mainmatter`` is part 0, titled 导读;
    the one-line abstract the book sets on a part's title page
    (``\\partabstract``) goes with the part.
    """

    def read(path: Path) -> str:
        return strip_comments(path.read_text(encoding="utf-8"))

    chapters: list[dict[str, Any]] = []
    part = [0]
    part_titles: dict[int, str] = {0: FRONT_TITLE}
    part_abstracts: dict[int, str] = {}
    pending_abstract = [""]
    appendix = [False]
    front = [0]

    def walk(text: str) -> None:
        pattern = (r"\\partabstract\{([^}]*)\}|\\part\{([^}]*)\}"
                   r"|\\(?:include|input)\{((?:chapters|integration|frontmatter)/[^}]+)\}|(\\appendix)\b")
        for match in re.finditer(pattern, text):
            if match.group(1) is not None:
                pending_abstract[0] = _plain(match.group(1))
                continue
            if match.group(4):
                appendix[0] = True
                continue
            if match.group(2) is not None:
                part[0] += 1
                part_titles[part[0]] = match.group(2).strip()
                part_abstracts[part[0]], pending_abstract[0] = pending_abstract[0], ""
                continue
            target = match.group(3)
            if target.startswith("integration/"):
                walk(read(book_root / (target if target.endswith(".tex") else f"{target}.tex")))
                continue
            stem = Path(target).stem
            if target.startswith("frontmatter/"):
                if part[0]:
                    raise TexParseError(f"主文件中 {stem} 位于 \\part 之后")
                front[0] += 1
                chapters.append({"stem": stem, "folder": "frontmatter", "part": 0, "part_title": "",
                                 "appendix": False, "front": True, "chapter": None, "label": f"F{front[0]}"})
                continue
            found = re.match(r"(\d+)-", stem)
            lettered = re.match(r"([A-Z])-", stem)
            if not part[0]:
                raise TexParseError(f"主文件中 {stem} 位于第一个 \\part 之前")
            chapters.append({"stem": stem, "folder": "chapters", "part": part[0], "part_title": "",
                             "appendix": appendix[0], "front": False,
                             "chapter": None if appendix[0] or not found else int(found.group(1)),
                             "label": found.group(1) if found else (lettered.group(1) if lettered else stem)})

    walk(read(book_root / MAIN_TEX))
    for chapter in chapters:
        chapter["part_title"] = part_titles[chapter["part"]]
        chapter["part_abstract"] = part_abstracts.get(chapter["part"], "")
    return chapters


def chapter_key(label: str) -> str:
    return f"{KEY_PREFIX}-{int(label):02d}" if label.isdigit() else f"{KEY_PREFIX}-{label}"


def parse_book(book_root: Path | str) -> dict[str, Any]:
    """Parts, chapters and lecture sections of the whole book."""

    root = Path(book_root)
    chapters: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[str] = []
    for spec in chapter_order(root):
        source_file = root / spec["folder"] / f"{spec['stem']}.tex"
        try:
            context = _Context(root / "chapters")
            events = scan_blocks(strip_comments(source_file.read_text(encoding="utf-8")), context)
            built = _assemble_chapter(spec, source_file.name, events)
        except (OSError, TexParseError) as error:
            issues.append(f"{source_file.name}：{error}")
            continue
        chapters.append(built["chapter"])
        sections.extend(built["sections"])
        issues.extend(f"{source_file.name}：{problem}" for problem in built["issues"])
        warnings.extend(f"{source_file.name}：{warning}" for warning in built["warnings"])
    for section in sections:
        residues = text_residues(section["blocks"])
        if residues:
            issues.append(f"{section['source_file']}（{section['title']}）：残留 TeX 片段：" + " ".join(residues))
    keys = [section["key"] for section in sections]
    for key in sorted({key for key in keys if keys.count(key) > 1}):
        issues.append(f"{key}：小节编号重复")
    return {"title": BOOK_TITLE, "chapters": chapters, "sections": sections, "issues": issues, "warnings": warnings}


def _empty_boxes(blocks: list[dict[str, Any]]) -> int:
    count = 0
    for block in blocks:
        if block["kind"] == "sentence":
            count += 0 if block["lines"] else 1
        elif block["kind"] in {"example", "exercise", "answer", "quote"}:
            count += 0 if block["blocks"] else 1
            count += _empty_boxes(block["blocks"])
        elif block["kind"] == "list":
            for item in block["items"]:
                count += _empty_boxes(item)
    return count


def _assemble_chapter(spec: dict[str, Any], source_file: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    key = chapter_key(spec["label"])
    title_event = next((event for event in events if event["k"] == "chapter"), None)
    if title_event is None:
        raise TexParseError("缺少 \\chapter 标题")
    if any(event["k"] == "chapter" for event in events[events.index(title_event) + 1:]):
        raise TexParseError("一个文件中出现了两个 \\chapter")
    chapter = {
        "key": key, "part": spec["part"], "part_title": spec["part_title"], "part_abstract": spec["part_abstract"],
        "appendix": spec["appendix"], "front": spec["front"], "chapter": spec["chapter"],
        "label": spec["label"], "title": title_event["title"], "source_file": source_file,
        "line": title_event["line"],
    }
    issues: list[str] = []
    warnings: list[str] = []
    units: list[dict[str, Any]] = [{"title": "", "events": [], "line": title_event["line"], "intro": True}]
    for event in events[events.index(title_event) + 1:]:
        if event["k"] == "section":
            if not event["numbered"]:
                raise TexParseError(f"第 {event['line']} 行：不编号的 \\section* 无法归入目录")
            units.append({"title": event["title"], "events": [], "line": event["line"], "intro": False})
            continue
        units[-1]["events"].append(event)
    sections: list[dict[str, Any]] = []
    numbered = 0
    for unit in units:
        if unit["intro"]:
            if not unit["events"] and len(units) > 1:
                continue
            section_key = f"{key}-00"
            title = "导言" if len(units) > 1 else chapter["title"]
        else:
            numbered += 1
            section_key = f"{key}-{numbered:02d}"
            title = unit["title"]
        try:
            blocks = to_blocks(unit["events"])
        except TexParseError as error:
            issues.append(f"（{title}）{error}")
            blocks = []
        empty = _empty_boxes(blocks)
        if empty:
            warnings.append(f"（{title}）有 {empty} 个空白的框或例句（原书源码中没有内容）")
        # In a section that sets exercises and then solves them, the solutions
        # start at the term that says 解答 (the page can fold them).
        answers_at = next((position for position, block in enumerate(blocks)
                           if block["kind"] == "term" and "解答" in runs_to_text(block["runs"])), None)
        sections.append({
            "key": section_key, "chapter_key": key, "part": spec["part"], "chapter": spec["chapter"],
            "title": title, "intro": unit["intro"], "numbered": not unit["intro"],
            "exercise": "练习" in title, "answers_at": answers_at if "练习" in title else None, "blocks": blocks, "source_file": source_file, "source_line": unit["line"],
        })
    chapter["section_keys"] = [section["key"] for section in sections]
    _number_boxes(chapter, sections)
    return {"chapter": chapter, "sections": sections, "issues": issues, "warnings": warnings}


def _number_boxes(chapter: dict[str, Any], sections: list[dict[str, Any]]) -> None:
    """The book's labels of the boxes: "Example 1.3.4" is chapter 1, section 3,
    the 4th example of the chapter (counted through the whole chapter);
    "Exercise 1.1" is the chapter's 1st exercise."""

    counts = {"example": 0, "exercise": 0}
    label = str(chapter["chapter"]) if chapter["chapter"] else str(chapter["label"])

    def walk(blocks: list[dict[str, Any]], section_number: int) -> None:
        for block in blocks:
            if block["kind"] in counts:
                counts[block["kind"]] += 1
                block["number"] = (f"{label}.{section_number}.{counts['example']}" if block["kind"] == "example"
                                   else f"{label}.{counts['exercise']}")
            walk(block.get("blocks", []), section_number)
            if block["kind"] == "list":
                for item in block["items"]:
                    walk(item, section_number)

    for section in sections:
        walk(section["blocks"], int(section["key"].rsplit("-", 1)[1]))


# ---------------------------------------------------------------------------
# Residues, statistics and pages

_RESIDUE = re.compile(
    r"\\|[{}]|\brd(?:[A-Z]\w*|key|seg|plainseg|raw|term|pattern|pill|color|translation|simpleexample|errorexample"
    r"|sentence|grammarline)\b|\bTL[a-z]+|linewidth|\[(?:-?[\d.]+\s*(?:em|ex|pt|cm|mm)|[lcr])\]"
)


def text_residues(value: Any) -> list[str]:
    """TeX left in any text of a block or run list (empty when clean)."""

    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"t", "text", "title", "label", "seg", "error", "name"} and isinstance(child, str):
                    found.extend(match.group(0) for match in _RESIDUE.finditer(child))
                elif isinstance(child, (list, dict)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return sorted(set(found))


def block_counts(blocks: Iterable[dict[str, Any]], counts: dict[str, int] | None = None) -> dict[str, int]:
    """How many blocks of each kind (nested ones included); analysed sentences
    and their parts are counted separately as ``analysed_lines`` / ``parts``."""

    counts = {} if counts is None else counts

    def lines(runs: list[dict[str, Any]]) -> None:
        sids = {run["sid"] for run in runs if "sid" in run}
        if sids:
            counts["analysed_lines"] = counts.get("analysed_lines", 0) + 1
            counts["parts"] = counts.get("parts", 0) + len(sids)

    for block in blocks:
        kind = block["kind"] + ("_error" if block.get("error") else "")
        counts[kind] = counts.get(kind, 0) + 1
        if block["kind"] == "sentence":
            for line in block["lines"]:
                lines(line["runs"])
        elif block["kind"] == "analysis":
            lines(block["runs"])
        elif block["kind"] == "table":
            for row in block["rows"]:
                for cell in row:
                    lines(cell)
        for child in ("blocks",):
            if child in block:
                block_counts(block[child], counts)
        for item in block.get("items", []) if block["kind"] == "list" else []:
            block_counts(item, counts)
    return counts


def _normal_title(title: str) -> str:
    return re.sub(r"[\s:：]+", "", unicodedata.normalize("NFKC", str(title or "")))


def locate(book: dict[str, Any], pdf_path: Path | str) -> dict[str, int]:
    """PDF pages of chapters and sections from the bookmarks (chapters are the
    level-2 entries, their sections the level-3 entries under them; the front
    matter's 总序 and 前言 are level-1 entries found by title)."""

    import pymupdf

    with pymupdf.open(str(pdf_path)) as document:
        toc = document.get_toc()
    chapter_pages: list[tuple[str, int, list[tuple[str, int]]]] = []
    front_pages = {_normal_title(title): page for level, title, page in toc if level == 1}
    counts = {"chapters": 0, "chapter_mismatch": 0, "sections": 0, "section_mismatch": 0}
    for chapter in [item for item in book["chapters"] if item.get("front")]:
        page = front_pages.get(_normal_title(chapter["title"]))
        if page is None:
            counts["chapter_mismatch"] += 1
            continue
        chapter["source_page"] = page
        counts["chapters"] += 1
        for section in book["sections"]:
            if section["chapter_key"] == chapter["key"]:
                section["source_page"], section["page_status"] = page, "bookmark"
    for level, title, page in toc:
        if level == 2:
            chapter_pages.append((title, page, []))
        elif level == 3 and chapter_pages:
            chapter_pages[-1][2].append((title, page))
    body = [chapter for chapter in book["chapters"] if not chapter.get("front")]
    if len(chapter_pages) != len(body):
        counts["chapter_mismatch"] += abs(len(chapter_pages) - len(body))
    for chapter, (title, page, bookmarks) in zip(body, chapter_pages):
        chapter["source_page"] = page
        counts["chapters"] += 1
        if _normal_title(title) != _normal_title(chapter["title"]):
            counts["chapter_mismatch"] += 1
        own = [section for section in book["sections"] if section["chapter_key"] == chapter["key"]]
        numbered = [section for section in own if section["numbered"]]
        for section in own:
            if not section["numbered"]:
                section["source_page"], section["page_status"] = page, "bookmark"
        if len(numbered) != len(bookmarks):
            counts["section_mismatch"] += abs(len(numbered) - len(bookmarks))
        for section, (bookmark_title, bookmark_page) in zip(numbered, bookmarks):
            same = _normal_title(bookmark_title) == _normal_title(section["title"])
            section["source_page"] = bookmark_page
            section["page_status"] = "bookmark" if same else "bookmark_title_differs"
            counts["sections"] += 1
            if not same:
                counts["section_mismatch"] += 1
    return counts


__all__ = [
    "BOOK_TITLE",
    "FRONT_TITLE",
    "KEY_PREFIX",
    "MAIN_TEX",
    "PART_NUMERALS",
    "PDF_NAME",
    "TexParseError",
    "analysed_parts",
    "analysed_sentences",
    "block_counts",
    "blocks_text",
    "chapter_key",
    "chapter_order",
    "locate",
    "parse_book",
    "parse_inline",
    "runs_to_text",
    "scan_blocks",
    "text_residues",
    "to_blocks",
]
