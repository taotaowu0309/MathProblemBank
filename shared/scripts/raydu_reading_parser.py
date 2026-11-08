"""Structured parser for the TeX source of 《旋元佑英文阅读》.

The book has two parts.  Part 1 (``chapters/part1-*.tex``) teaches the
question types: each section has a lecture (答题重点, 常见问法, 说明) and one
or more worked examples (范例 passage, 例题, 翻译, 题解).  Part 2
(``chapters/part2-chapter-NN-<kind>.tex``) holds the practice tests: in every
chapter a mainstream, special, TOEFL and IELTS part, each made of passages
with numbered questions, an answer key, the Chinese translation and the
book's explanation of every question.

The source is typeset for print, so besides the semantic macros it is full of
page-layout commands (``\\Needspace``, minipages, measured groups).  The
parser works in two steps:

1. :func:`scan_blocks` turns TeX into a flat list of block *events*
   (paragraph, heading, choices, answer line, solution box, table ...).
   Layout-only commands are dropped by an explicit list; every other command
   must be known, so a source change can never leak TeX into the program.
   ``\\ReadGroupStartNeedspace{id}{...}`` typesets a throw-away copy of the
   following text to measure it; that copy is discarded.
2. The assemblers group the events into passages and questions and pair each
   question with its answer, Chinese text and explanation.

Rich text is stored as *runs* (as for 《文法解题》): ``{"t": text}`` plus style
keys ``b`` (bold), ``i`` (italic), ``u`` (underline), ``em`` (the book's bold
green key words), ``g`` (green text), ``wave`` (wavy underline marking a
question type), ``dot`` (dotted underline), ``hl`` (highlighted words in a
TOEFL passage), ``sup``; and the special runs ``{"br": True}`` (paragraph
break), ``{"line": True}`` (line break), ``{"blank": True, "n": 3}`` (a gap),
``{"box": "A"}`` (a sentence-insertion square), ``{"circ": 2}`` (a circled
paragraph number) and ``{"table": [[runs, ...], ...]}``.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

QUESTION_CODE_PREFIX = "XYRD"
BOOK_TITLE = "旋元佑英文阅读"
PDF_NAME = "旋元佑英文阅读.pdf"

READING_CATEGORIES = {
    "mainstream": ("MS", "主流阅读测验"),
    "special": ("SP", "特殊阅读测验"),
    "toefl": ("TF", "托福阅读测验"),
    "ielts": ("IE", "雅思阅读测验"),
}
FILE_KINDS = {
    "mainstream": "mainstream", "item3": "mainstream", "special": "special",
    "search": "special", "toefl": "toefl", "ielts": "ielts",
}

# Question types of the reading book (``question_type`` in the bank).
TYPE_NAMES = {
    "reading_choice": "阅读选择",
    "gap_select": "选择填空",
    "heading_match": "小标题",
    "true_false_ng": "真伪判断",
    "multi_select": "复选主题",
    "word_fill": "限定字数填空",
}

# The book's own names of question types, normalised: the explanation marks
# the type with a wavy underline ("这是\\CJKunderwave{字汇题}").
TOPIC_ALIASES = {
    "主题型": "主题型", "主题题": "主题型", "主旨题": "主题型",
    "细节题": "细节题", "细节题变型": "细节题变型", "细节题的变型": "细节题变型", "细节变型": "细节题变型",
    "推论题": "推论题", "推论题变型": "推论题变型", "推论题的变型": "推论题变型", "推论变型": "推论题变型",
    "字汇题": "字汇题", "语气态度题": "语气态度题", "语气题": "语气态度题", "态度题": "语气态度题",
    "修辞目的": "修辞目的题", "修辞目的题": "修辞目的题",
    "组织结构": "组织结构题", "组织结构题": "组织结构题",
    "安插句子": "安插句子题", "安插句子题": "安插句子题", "插入句": "安插句子题",
    "句子简化": "句子简化题", "句子简化题": "句子简化题",
    "指涉题": "指涉题", "指涉": "指涉题",
    "选句填空": "选句填空", "查找出处": "查找出处", "段落重组": "段落重组", "小标题": "小标题",
    "配对": "配对", "配对题": "配对", "复选主题": "复选主题",
    "真伪判断": "真伪判断", "限定字数填空": "限定字数填空",
    "笔记型": "限定字数填空", "表格式": "限定字数填空", "流程图": "限定字数填空", "摘要型": "限定字数填空",
}
TYPE_DEFAULT_TOPICS = {
    "true_false_ng": "真伪判断", "word_fill": "限定字数填空", "heading_match": "小标题",
}

TFNG_VALUES = ("TRUE", "FALSE", "NOT GIVEN", "YES", "NO")
ROMAN = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv")


class TexParseError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Low-level TeX reading

def _read_group(text: str, start: int) -> tuple[str, int]:
    """The content of the ``{...}`` group opening at ``start`` and the index after it."""

    if start >= len(text) or text[start] != "{":
        raise TexParseError(f"缺少花括号参数：{text[start:start + 30]!r}")
    depth = 0
    index = start
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    raise TexParseError("花括号不成对")


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _read_optional(text: str, index: int) -> tuple[str | None, int]:
    """An optional ``[...]`` argument (bracket depth aware)."""

    position = _skip_spaces(text, index)
    if position >= len(text) or text[position] != "[":
        return None, index
    depth = 0
    cursor = position
    while cursor < len(text):
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return text[position + 1:cursor], cursor + 1
        elif character == "{":
            _content, cursor = _read_group(text, cursor)
            continue
        cursor += 1
    raise TexParseError("方括号不成对")


def _read_args(text: str, index: int, count: int) -> tuple[list[str], int]:
    args: list[str] = []
    for _ in range(count):
        position = _skip_spaces(text, index)
        if position < len(text) and text[position] == "{":
            value, index = _read_group(text, position)
        elif position < len(text) and text[position] == "\\":
            # A single control sequence argument, e.g. \let\a\b or \ReadGap\foo.
            match = re.match(r"\\(?:[A-Za-z@]+|.)", text[position:])
            value, index = match.group(0), position + match.end()
        elif position < len(text):
            value, index = text[position], position + 1
        else:
            raise TexParseError("缺少命令参数")
        args.append(value)
    return args, index


def _skip_dimension(text: str, index: int) -> int:
    """Skip an ``=value`` or ``value`` TeX assignment such as ``\\hangindent=3em``."""

    # Only a number with a unit (or a dimension register) belongs to the assignment:
    # in "\\hangafter=1\\makebox[3em]{...}" the \\makebox is the next command.
    register = r"\\(?:linewidth|textwidth|columnwidth|baselineskip|parindent|z@)"
    match = re.match(
        r"\s*=?\s*(?:-?(?:\d+\.?\d*|\.\d+)\s*(?:em|ex|pt|cm|mm|in|bp|sp|" + register + r")?|" + register + r")"
        r"(?:\s*plus\s*[\d.]+\s*\w+)?",
        text[index:],
    )
    return index + (match.end() if match else 0)


def strip_comments(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        match = re.search(r"(?<!\\)%", line)
        lines.append(line[:match.start()] if match else line)
    return "\n".join(lines)


def _find_environment_end(text: str, name: str, start: int) -> tuple[int, int]:
    """Index of the matching ``\\end{name}`` (nesting of the same name aware)."""

    begin_token = f"\\begin{{{name}}}"
    end_token = f"\\end{{{name}}}"
    depth = 1
    index = start
    while True:
        next_begin = text.find(begin_token, index)
        next_end = text.find(end_token, index)
        if next_end < 0:
            raise TexParseError(f"环境 {name} 缺少 \\end")
        if 0 <= next_begin < next_end:
            depth += 1
            index = next_begin + len(begin_token)
            continue
        depth -= 1
        if depth == 0:
            return next_end, next_end + len(end_token)
        index = next_end + len(end_token)


# ---------------------------------------------------------------------------
# Inline rich text

_IGNORED_INLINE = {
    "noindent", "smallskip", "medskip", "bigskip", "normalfont", "normalsize", "small", "footnotesize",
    "large", "Large", "selectfont", "rmfamily", "sffamily", "bfseries", "mdseries", "upshape",
    "allowbreak", "nobreak", "strut", "relax", "ReadSolutionMark", "centering", "raggedright",
    "sloppy", "ReadBodyFont", "ReadTableFont", "ReadAnswerGuard", "break", "linebreak", "nopagebreak",
    "phantomsection", "hss", "null", "ignorespaces", "unskip", "leavevmode", "par@", "bf", "protect",
    "ReadSolutionContexttrue", "ReadSolutionContextfalse", "itshape",
}
_SPACE_INLINE = {"quad": "\u2003", "qquad": "\u2003\u2003", "enspace": " ", "thinspace": " ", "hfill": "\u2003\u2003",
                 "space": " ", "enskip": " ", "quad@": " "}
_SYMBOLS = {
    "rightarrow": "→", "to": "→", "leftarrow": "←", "downarrow": "↓", "uparrow": "↑", "Rightarrow": "⇒",
    "textbullet": "•", "ReadBullet": "•", "ldots": "…", "dots": "…", "cdots": "⋯", "times": "×",
    "blacktriangleright": "▶", "blacklozenge": "◆", "textdegree": "°", "S": "§", "checkmark": "✓",
    "ReadTimesApostrophe": "’", "ReadTimesOpenQuote": "“", "ReadTimesCloseQuote": "”",
    "ReadTimesEmDash": "—", "ReadAsciiSemicolon": ";", "textemdash": "—", "textendash": "–",
    "textquoteright": "’", "textquoteleft": "‘", "textquotedblleft": "“", "textquotedblright": "”",
    "ReadChNineMarkedDash": "—", "ReadChNineMarkedApostrophe": "’", "ReadChTenToeflMarkedSemicolon": ";",
    "pounds": "£", "textpm": "±", "pm": "±", "geq": "≥", "leq": "≤", "neq": "≠", "approx": "≈",
    "circ": "°", "cdot": "·", "textperiodcentered": "·", "dag": "†", "textasteriskcentered": "*",
    "slash": "/", "textbackslash": "\\", "&": "&",
}
# Style command -> run keys.
_STYLES = {
    "textbf": {"b": True}, "textit": {"i": True}, "emph": {"i": True}, "textsl": {"i": True},
    "underline": {"u": True}, "uline": {"u": True}, "uuline": {"u": True}, "ReadUnderlined": {"u": True},
    "CJKunderdblline": {"u": True}, "ReadTableHead": {"b": True},
    "ReadChapterOneUnderlined": {"u": True, "g": True},
    "ReadChapterOneWave": {"wave": True}, "ReadChNineWave": {"wave": True}, "ReadChTenWave": {"wave": True},
    "ReadChapterOneDottedUnder": {"dot": True}, "ReadChNineDot": {"dot": True}, "ReadChTenDot": {"dot": True},
    "ReadGreenWord": {"em": True}, "ReadHighlight": {"hl": True}, "ReadHighlightBreakable": {"hl": True},
    "hl": {"hl": True}, "textsuperscript": {"sup": True}, "textsubscript": {"sub": True},
    "ReadLatinFont": {}, "ReadAsciiPunctFont": {}, "mbox": {}, "hbox": {}, "text": {}, "textrm": {},
    "textsf": {}, "texttt": {}, "textnormal": {}, "textup": {}, "ensuremath": {}, "ReadChThreeFishGlyphs": {},
    "textmd": {}, "fbox": {}, "shortstack": {}, "mathrm": {}, "mathbf": {"b": True}, "mathit": {"i": True},
}
_COLOR_STYLES = {"ReadGreen": {"g": True}, "ReadGreenDark": {"g": True}}
_ESCAPED = {"%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", " ": " ",
            ",": "\u2009", ";": " ", ":": " ", "!": "", "/": "", "-": "", "@": "", "*": "", "\n": " "}


def _circled(number: str) -> str:
    try:
        value = int(number)
    except ValueError:
        return f"({number})"
    return chr(0x2460 + value - 1) if 1 <= value <= 20 else f"({value})"


def parse_inline(source: str) -> list[dict[str, Any]]:
    """Convert inline TeX of the reading book into style runs."""

    runs: list[dict[str, Any]] = []

    def walk(text: str, style: dict[str, Any], math: bool = False) -> None:
        index = 0
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                runs.append({"t": "".join(buffer), **style})
                buffer.clear()

        while index < len(text):
            character = text[index]
            if character == "\\":
                if text.startswith("\\(", index):
                    end = text.find("\\)", index + 2)
                    if end < 0:
                        raise TexParseError("数学模式 \\( 不成对")
                    flush()
                    walk(text[index + 2:end], style, True)
                    index = end + 2
                    continue
                match = re.match(r"\\([A-Za-z@]+)\*?", text[index:])
                if match is None:
                    following = text[index + 1:index + 2]
                    if following == "\\":
                        flush()
                        runs.append({"line": True})
                        index += 2
                        index = _skip_spaces(text, index)
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
                if name in _SPACE_INLINE:
                    buffer.append(_SPACE_INLINE[name])
                    continue
                if name in _SYMBOLS:
                    buffer.append(_SYMBOLS[name])
                    if text.startswith("{}", index):
                        index += 2
                    elif index < len(text) and text[index] == " " and name.startswith("Read"):
                        pass
                    continue
                if name in {"par", "newline"}:
                    flush()
                    runs.append({"br": True} if name == "par" else {"line": True})
                    continue
                if name in {"hspace", "vspace", "addvspace", "label", "ReadTranslationLabel", "ReadTranslationRef",
                            "Needspace", "needspace", "thispagestyle", "pagestyle", "arrayrulecolor",
                            "cellcolor", "rowcolor", "cline", "xeCJKsetup", "pdfbookmark", "index", "hypertarget",
                            "phantom", "vphantom", "hphantom", "fontsize", "fontspec"}:
                    index = _skip_spaces(text, index)
                    if text.startswith("*", index):
                        index += 1
                    _optional, index = _read_optional(text, index)
                    count = 2 if name in {"fontsize", "hypertarget"} else 1
                    values, index = _read_args(text, index, count)
                    if name == "hspace" or name == "phantom":
                        buffer.append(" ")
                    if name in {"pdfbookmark"}:
                        _rest, index = _read_args(text, index, 1)
                    continue
                if name in {"pageref", "ref"}:
                    (label,), index = _read_args(text, index, 1)
                    flush()
                    runs.append({"pageref": label.strip()})
                    continue
                if name == "hyperref":
                    _target, index = _read_optional(text, index)
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(content, style, math)
                    continue
                if name in {"kern", "hangindent", "hangafter", "parindent", "emergencystretch", "tabcolsep",
                            "clubpenalty", "widowpenalty", "interlinepenalty", "displaywidowpenalty",
                            "arraystretch", "baselineskip", "parskip", "leftskip", "rightskip", "fboxsep",
                            "lineskip", "spaceskip", "raggedbottom"}:
                    index = _skip_dimension(text, index)
                    continue
                if name in {"setlength", "addtolength"}:
                    _values, index = _read_args(text, index, 2)
                    continue
                if name in {"color", "rule"}:
                    _optional, index = _read_optional(text, index)
                    values, index = _read_args(text, index, 1 if name == "color" else 2)
                    if name == "color":
                        flush()
                        style = {**style, **_COLOR_STYLES.get(values[0].strip(), {})}
                    continue
                if name in {"textcolor", "colorbox"}:
                    _optional, index = _read_optional(text, index)
                    (color, content), index = _read_args(text, index, 2)
                    flush()
                    extra = _COLOR_STYLES.get(color.strip(), {})
                    if name == "colorbox":
                        extra = {"hl": True}
                    walk(content, {**style, **extra}, math)
                    continue
                if name in {"makebox", "framebox", "raisebox", "parbox"}:
                    if name == "raisebox":
                        _values, index = _read_args(text, index, 1)
                    elif name == "parbox":
                        _optional, index = _read_optional(text, index)
                        _values, index = _read_args(text, index, 1)
                    for _ in range(2):
                        _optional, index = _read_optional(text, index)
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(content, style, math)
                    continue
                if name in {"CJKunderline", "CJKunderwave", "CJKunderdot", "CJKunderanyline", "CJKsout",
                            "CJKunderdblline"}:
                    _optional, index = _read_optional(text, index)
                    if name == "CJKunderanyline":
                        _values, index = _read_args(text, index, 2)
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    extra = {"CJKunderline": {"u": True}, "CJKunderwave": {"wave": True}, "CJKunderdblline": {"u": True},
                             "CJKunderdot": {"dot": True}, "CJKunderanyline": {"dot": True},
                             "CJKsout": {"s": True}}[name]
                    walk(content, {**style, **extra}, math)
                    continue
                if name == "leaders":
                    # A dotted separator rule: \leaders\hbox to 1em{.}\hfill.
                    skip = re.match(r"\s*\\hbox\s*to\s*[\d.]+\s*\w+\s*", text[index:])
                    if skip:
                        index += skip.end()
                        _box, index = _read_args(text, index, 1)
                    fill = re.match(r"\s*\\(?:hfill|hrulefill|dotfill)", text[index:])
                    if fill:
                        index += fill.end()
                    continue
                if name in {"ReadBoxLetter"}:
                    (letter,), index = _read_args(text, index, 1)
                    flush()
                    runs.append({"box": _plain_inline(letter)})
                    continue
                if name == "ReadCircled":
                    (number,), index = _read_args(text, index, 1)
                    flush()
                    runs.append({"circ": _plain_inline(number)})
                    continue
                if name in {"ReadTocCircled", "ovnum", "textcircled", "ReadFilledCircled"}:
                    (number,), index = _read_args(text, index, 1)
                    buffer.append(_circled(_plain_inline(number)))
                    continue
                if name in {"ReadGap"}:
                    (label,), index = _read_args(text, index, 1)
                    flush()
                    number = re.sub(r"\D", "", _plain_inline(label))
                    runs.append({"blank": True, **({"n": int(number)} if number else {})})
                    continue
                if name in {"ReadDottedBlank", "dotfill", "ReadSummaryBlank"}:
                    if name == "ReadDottedBlank":
                        _optional, index = _read_optional(text, index)
                    flush()
                    if name == "ReadSummaryBlank":
                        runs.append({"t": "◆ ", **style})
                    runs.append({"blank": True})
                    continue
                if name == "ReadStepArrows":
                    flush()
                    runs.append({"br": True})
                    runs.append({"t": "↓\u2003↓\u2003↓", "g": True, "center": True})
                    runs.append({"br": True})
                    continue
                if name in {"multicolumn", "multirow"}:
                    (_span, _spec, content), index = _read_args(text, index, 3)
                    flush()
                    walk(content, style, math)
                    continue
                if name in {"ReadAnswerLine", "ReadAnswerLinePlain", "ReadAnswerText"}:
                    (number, value), index = _read_args(text, index, 2)
                    flush()
                    label = _plain_inline(number).rstrip(".")
                    shown = f"({_plain_inline(value)})" if name == "ReadAnswerLine" else _plain_inline(value)
                    runs.append({"t": f"{label}. " if label else "", **style})
                    runs.append({"t": shown, **style, "g": True})
                    runs.append({"line": True})
                    continue
                if name in _STYLES:
                    if index < len(text) and text[index] == "[":
                        _optional, index = _read_optional(text, index)
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    if _STYLES[name].get("u") and not parse_inline(content):
                        # \underline{\makebox[12em]{}}: an answer blank.
                        runs.append({"blank": True})
                        continue
                    walk(content, {**style, **_STYLES[name]}, math)
                    continue
                if math and name in {"mathrm", "mathbf", "text", "mbox"}:
                    (content,), index = _read_args(text, index, 1)
                    flush()
                    walk(content, style, math)
                    continue
                raise TexParseError(f"无法识别的命令：\\{name}")
            if character == "{":
                content, index = _read_group(text, index)
                flush()
                walk(content, dict(style), math)
                continue
            if character == "}":
                raise TexParseError("多余的右花括号")
            if character == "$":
                end = text.find("$", index + 1)
                if end < 0:
                    raise TexParseError("数学模式 $ 不成对")
                flush()
                walk(text[index + 1:end], style, True)
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
            elif character == "`":
                buffer.append("‘")
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
            elif math and character in "^_":
                pass
            else:
                buffer.append(character)
            index += 1
        flush()

    walk(str(source or ""), {})
    return normalize_runs(runs)


def _plain_inline(source: str) -> str:
    return runs_to_text(parse_inline(source)).strip()


_SPECIAL_RUN_KEYS = ("br", "line", "blank", "box", "circ", "table", "pageref")


def _is_special(run: dict[str, Any]) -> bool:
    return any(key in run for key in _SPECIAL_RUN_KEYS)


def _style(run: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(sorted((key, value) for key, value in run.items() if key != "t"))


def normalize_runs(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge equal-style neighbours, collapse spaces and trim breaks at the ends."""

    merged: list[dict[str, Any]] = []
    for run in runs:
        if run.get("br") or run.get("line"):
            if merged and not (merged[-1].get("br") or merged[-1].get("line")):
                merged.append({"br": True} if run.get("br") else {"line": True})
            elif merged and run.get("br") and merged[-1].get("line"):
                merged[-1] = {"br": True}
            continue
        if "t" in run and not _is_special(run):
            text = re.sub(r"[ \t\r\n]+", " ", str(run["t"]))
            run = {**run, "t": text}
            if merged and "t" in merged[-1] and not _is_special(merged[-1]) and _style(merged[-1]) == _style(run):
                merged[-1] = {**merged[-1], "t": merged[-1]["t"] + text}
                continue
        merged.append(dict(run))
    previous_space = True
    for run in merged:
        if run.get("br") or run.get("line"):
            previous_space = True
            continue
        if "t" not in run:
            previous_space = False
            continue
        text = run["t"]
        if previous_space:
            text = text.lstrip(" ")
        run["t"] = text
        previous_space = text.endswith(" ") if text else previous_space
    cleaned = [run for run in merged if _is_special(run) or run.get("t")]
    while cleaned and (cleaned[0].get("br") or cleaned[0].get("line")):
        cleaned.pop(0)
    while cleaned and (cleaned[-1].get("br") or cleaned[-1].get("line")):
        cleaned.pop()
    for index, run in enumerate(cleaned):
        if "t" not in run:
            continue
        if index == 0:
            run["t"] = run["t"].lstrip()
        if index == len(cleaned) - 1 or cleaned[index + 1].get("br") or cleaned[index + 1].get("line"):
            run["t"] = run["t"].rstrip()
    return [run for run in cleaned if _is_special(run) or run.get("t")]


def runs_to_text(runs: Iterable[dict[str, Any]], *, blank: str = "_____") -> str:
    parts: list[str] = []
    for run in runs or []:
        if run.get("br") or run.get("line"):
            parts.append("\n")
        elif run.get("blank"):
            parts.append(f"__{run['n']}__" if run.get("n") else blank)
        elif "box" in run:
            parts.append(f"[{run['box']}]")
        elif "pageref" in run:
            parts.append(str(run.get("page") or "?"))
        elif "circ" in run:
            parts.append(_circled(str(run["circ"])) + " ")
        elif "table" in run:
            parts.append("\n" + "\n".join(
                " | ".join(runs_to_text(cell) for cell in row) for row in run["table"]
            ) + "\n")
        else:
            parts.append(str(run.get("t") or ""))
    lines = [re.sub(r"[ \u00a0\u2009]+", " ", line).strip() for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Block events

# Block macros: name -> (number of mandatory arguments, event kind).
_BLOCK_MACROS = {
    "ReadPartSectionTitle": (2, "section"),
    "ReadChapterSubheading": (1, "unit"),
    "ReadChapterEightMainHeading": (1, "unit_number"),
    "ReadChapterNineMainHeading": (1, "unit_number"),
    "ReadChapterTenMainHeading": (1, "unit_number"),
    "ReadChTenIeltsTitle": (2, "article_title2"),
    "ReadArticleTitle": (1, "title"),
    "ReadSubheading": (1, "subheading"),
    "ReadSectionLetter": (1, "letter"),
    "ReadPill": (1, "pill"),
    "ReadLabel": (1, "label"),
    "ReadTranslationHeading": (0, "translation"),
    "ReadQuestionsStart": (0, "questions_start"),
    "ReadAnswerLine": (2, "answer"),
    "ReadAnswerLinePlain": (2, "answer_plain"),
    "ReadAnswerText": (2, "answer_text"),
    "ReadChapterTitle": (1, "chapter_title"),
    "ReadGroupHeading": (1, "group_heading"),
    "ReadTypeHeading": (3, "type_heading"),
    "ReadVariantHeading": (2, "variant_heading"),
    "ReadChapterTypeHeading": (3, "type_heading"),
    "ReadStandaloneHeading": (1, "standalone_heading"),
    "ReadPartChapterDivider": (1, "drop"),
    "ReadPartTitle": (1, "drop"),
    "ReadStepArrows": (0, "arrows"),
}
# Layout commands that are dropped with their arguments: name -> argument count.
_DROP_MACROS = {
    "Needspace": 1, "needspace": 1, "ReadTranslationLabel": 1, "thispagestyle": 1,
    "vspace": 1, "addvspace": 1, "setlength": 2, "addtolength": 2, "xeCJKsetup": 1, "arrayrulecolor": 1,
    "pagestyle": 1, "ReadTranslationRef": 1, "enlargethispage": 1, "pdfbookmark": 2, "addcontentsline": 3,
    "markright": 1, "markboth": 2, "noalign": 1, "tcbset": 1, "soulregister": 2,
}
_DROP_PLAIN = {
    "par", "medskip", "smallskip", "bigskip", "noindent", "indent", "clearpage", "newpage", "pagebreak",
    "nopagebreak", "nobreak", "ReadSolutionContexttrue", "ReadSolutionContextfalse", "sloppy", "raggedright",
    "centering", "normalsize", "ReadBodyFont", "ReadTableFont", "ReadQuestionGap", "ReadResumeBodyLeading",
    "begingroup", "endgroup", "ReadAnswerGuard", "relax", "small", "footnotesize", "leavevmode", "fussy",
    "ReadPartIIIntro", "ReadSolutionMark", "hline", "toprule", "midrule", "bottomrule", "makeatletter",
    "makeatother", "phantomsection", "strut", "raggedleft", "flushbottom", "itshape", "normalfont",
}
_DROP_ASSIGN = {
    "hangindent", "hangafter", "parindent", "emergencystretch", "tabcolsep", "arraystretch", "kern",
    "parskip", "baselineskip", "fboxsep", "vskip", "hskip", "leftskip", "rightskip",
    "clubpenalty", "widowpenalty", "interlinepenalty", "displaywidowpenalty",
}
_TRANSPARENT_ENVS = {
    "minipage", "readinggroupstart", "readingquestiongroupstart", "readingshortquestion", "center",
    "flushleft", "flushright", "readingtranslation", "readingquote", "raggedright", "samepage", "tcolorbox",
}
_TABLE_ENVS = {"tabular", "tabularx", "tabular*", "longtable", "tblr"}


def _definition_end(text: str, index: int, name: str) -> int:
    """Skip a ``\\newcommand``/``\\renewcommand``/``\\renewenvironment``/``\\let``/``\\def``."""

    if name == "let":
        _args, index = _read_args(text, index, 2)
        return index
    if name == "def":
        match = re.match(r"\s*\\[A-Za-z@]+(?:#\d)*", text[index:])
        index += match.end() if match else 0
        _args, index = _read_args(text, index, 1)
        return index
    position = _skip_spaces(text, index)
    if text.startswith("*", position):
        position += 1
    _name, index = _read_args(text, position, 1)
    _optional, index = _read_optional(text, index)
    _optional, index = _read_optional(text, index)
    count = 2 if name in {"newenvironment", "renewenvironment"} else 1
    _bodies, index = _read_args(text, index, count)
    return index


def _contains_block(text: str) -> bool:
    return bool(re.search(
        r"\\(?:par\b|begin\{|" + "|".join(sorted(_BLOCK_MACROS, key=len, reverse=True)) + r")", text
    ))


def scan_blocks(text: str, *, line_offset: int = 0) -> list[dict[str, Any]]:
    """Turn TeX into a flat list of block events (see the module docstring)."""

    events: list[dict[str, Any]] = []
    buffer: list[str] = []
    buffer_start = [0]
    # \label{...} anchors are attached to the next paragraph (for \pageref).
    anchors: list[str] = []
    index = 0

    def line_of(position: int) -> int:
        return line_offset + text.count("\n", 0, position) + 1

    def flush() -> None:
        raw = "".join(buffer)
        buffer.clear()
        if not raw.strip():
            return
        try:
            runs = parse_inline(raw)
        except TexParseError as error:
            raise TexParseError(f"第 {line_of(buffer_start[0])} 行：{error}") from None
        # "(\ReadTranslationRef{...})" leaves empty brackets: a print-only cross reference.
        if runs and runs_to_text(runs) not in {"()", "（）"}:
            event = {"k": "para", "runs": runs, "raw": raw, "line": line_of(buffer_start[0])}
            if anchors:
                event["anchors"] = list(anchors)
                anchors.clear()
            events.append(event)

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
        name = match.group(1)
        after = index + match.end()
        position = index
        if name in {"newcommand", "renewcommand", "providecommand", "newenvironment", "renewenvironment",
                    "let", "def"}:
            index = _definition_end(text, after, name)
            continue
        if name in {"newCJKfontfamily", "newfontfamily"}:
            _target, index = _read_args(text, after, 1)
            _optional, index = _read_optional(text, index)
            _font, index = _read_args(text, index, 1)
            continue
        if re.fullmatch(r"Read[A-Za-z]+(?:true|false)", name):
            # A layout switch such as \ReadChapterPendingfalse.
            index = after
            continue
        if name == "label":
            (label,), index = _read_args(text, after, 1)
            anchors.append(label.strip())
            continue
        if name == "ReadGroupStartNeedspace":
            # The second argument is a measured, discarded copy of what follows.
            _args, index = _read_args(text, after, 2)
            continue
        if name == "begin":
            (environment,), body_start = _read_args(text, after, 1)
            environment = environment.strip()
            body_end, index = _find_environment_end(text, environment, body_start)
            body = text[body_start:body_end]
            flush()
            if environment in _TRANSPARENT_ENVS:
                body = _strip_environment_arguments(environment, body)
                events.extend(scan_blocks(body, line_offset=line_of(body_start) - 1))
            elif environment == "readinganswerkey":
                events.append({"k": "key", "raw": body, "line": line_of(position),
                               "events": scan_blocks(body, line_offset=line_of(body_start) - 1)})
            elif environment == "readinganswerbox":
                inner = scan_blocks(body, line_offset=line_of(body_start) - 1)
                events.append({"k": "box", "runs": events_to_runs(inner), "line": line_of(position)})
            elif environment in {"readchoices", "readoptions"}:
                events.append({"k": "choices", "line": line_of(position),
                               **_parse_choices(environment, body, line_of(body_start))})
            elif environment in {"itemize", "enumerate", "description"}:
                body = body[_read_optional(body, 0)[1]:] if body.lstrip().startswith("[") else body
                counter = re.search(r"\\setcounter\{enumi\}\{(\d+)\}", body)
                body = re.sub(r"\\setcounter\{enumi\}\{\d+\}", "", body)
                items = []
                for item in re.split(r"\\item\b", body)[1:]:
                    item_events = scan_blocks(item, line_offset=line_of(body_start) - 1)
                    # Lines of one item (an English question and its Chinese) are not paragraphs.
                    items.append([{"line": True} if run.get("br") else run for run in events_to_runs(item_events)])
                events.append({"k": "list", "items": items, "ordered": environment == "enumerate",
                               "start": int(counter.group(1)) + 1 if counter else 1,
                               "line": line_of(position)})
            elif environment in _TABLE_ENVS:
                events.append({"k": "table", "rows": _parse_table(environment, body), "line": line_of(position)})
            elif environment == "tikzpicture":
                events.append({"k": "tikz", "raw": body, "line": line_of(position)})
            else:
                raise TexParseError(f"第 {line_of(position)} 行：无法识别的环境 {environment}")
            continue
        if name == "end":
            raise TexParseError(f"第 {line_of(position)} 行：多余的 \\end")
        if name in _BLOCK_MACROS:
            count, kind = _BLOCK_MACROS[name]
            args, index = _read_args(text, after, count) if count else ([], after)
            flush()
            if kind == "unit_number":
                # "\ReadChapterEightMainHeading{1}" typesets "阅读测验 ①".
                events.append({"k": "unit", "args": args, "line": line_of(position),
                               "text": [f"阅读测验 {_circled(_plain_inline(args[0]))}"]})
            elif kind != "drop":
                events.append({"k": kind, "args": args, "line": line_of(position),
                               "text": [_plain_inline(arg) for arg in args]})
            continue
        if name in _DROP_MACROS:
            index = after
            if text.startswith("*", index):
                index += 1
            _optional, index = _read_optional(text, index)
            _args, index = _read_args(text, index, _DROP_MACROS[name])
            if name == "par":
                flush()
            continue
        if name in _DROP_PLAIN:
            if name == "par":
                flush()
            index = after
            continue
        if name in _DROP_ASSIGN:
            index = _skip_dimension(text, after)
            continue
        if name == "renewcommand":
            index = _definition_end(text, after, name)
            continue
        if name in {"makebox", "parbox", "centerline", "mbox", "fbox"}:
            # A wrapper whose argument holds block content (e.g. a table): scan the content.
            cursor = after
            if name == "parbox":
                _optional, cursor = _read_optional(text, cursor)
            for _ in range(2 if name == "makebox" else 0):
                _optional, cursor = _read_optional(text, cursor)
            if name == "parbox":
                _width, cursor = _read_args(text, cursor, 1)
            peek = _skip_spaces(text, cursor)
            if peek < len(text) and text[peek] == "{":
                content, end = _read_group(text, peek)
                if _contains_block(content):
                    flush()
                    events.extend(scan_blocks(content, line_offset=line_of(peek) - 1))
                    index = end
                    continue
        # Inline command: keep it (and its arguments) for parse_inline.
        append(text[position:after], position)
        index = after
    flush()
    return events


def _strip_environment_arguments(environment: str, body: str) -> str:
    """Drop the leading arguments of a transparent environment."""

    index = 0
    if environment == "minipage":
        _optional, index = _read_optional(body, index)
        _optional, index = _read_optional(body, index)
        _optional, index = _read_optional(body, index)
        _args, index = _read_args(body, index, 1)
    elif environment == "tcolorbox":
        _optional, index = _read_optional(body, index)
    elif environment in {"readinggroupstart", "readingquestiongroupstart"}:
        _optional, index = _read_optional(body, index)
        _args, index = _read_args(body, index, 1)
    return body[index:]


def _parse_choices(environment: str, body: str, line: int) -> dict[str, Any]:
    """``readchoices`` rows (\\ReadChoicePair / \\ReadChoiceLine) or ``readoptions`` paragraphs."""

    options: list[dict[str, Any]] = []
    layout = "single"
    if environment == "readchoices":
        index = 0
        cells: list[str] = []
        while True:
            match = re.search(r"\\ReadChoice(Pair|Line)\b", body[index:])
            if match is None:
                break
            kind = match.group(1)
            args, index = _read_args(body, index + match.end(), 2 if kind == "Pair" else 1)
            cells.extend(args)
            if kind == "Pair":
                layout = "two_column"
        for cell in cells:
            runs = parse_inline(cell)
            if not runs:
                continue
            insert = split_insert_options(runs)
            if insert:
                options.extend(insert[1])
                layout = "two_column"
                continue
            options.append(_option_from_runs(runs, line, strict=False))
    else:
        for event in scan_blocks(body, line_offset=line - 1):
            if event["k"] != "para":
                raise TexParseError(f"第 {line} 行：选项区出现了非段落内容")
            options.append(_option_from_runs(event["runs"], line))
    options.sort(key=lambda option: label_order(option["label"]))
    return {"options": options, "layout": layout}


def label_order(label: str) -> tuple[int, int, str]:
    if label in ROMAN:
        return (1, ROMAN.index(label), label)
    if label.isdigit():
        return (2, int(label), label)
    return (0, len(label), label)


def _option_from_runs(runs: list[dict[str, Any]], line: int, *, strict: bool = True) -> dict[str, Any]:
    text = runs_to_text(runs)
    match = re.match(r"^\(([A-Ha-h])\)\s*|^([A-H])\.\s+|^(\d{1,2})\.\s+|^([ivx]+)\.\s+", text)
    if match is None and not strict:
        # A cell without a label, e.g. the "Example / Answer" cell of an IELTS question table.
        return {"label": "", "runs": runs}
    if match is None:
        raise TexParseError(f"第 {line} 行：选项缺少 (A)–(H)、数字或罗马数字标号：{text[:40]}")
    label = match.group(4) or (match.group(1) or match.group(2) or match.group(3)).upper()
    return {"label": label, "runs": strip_prefix(runs, match.end())}


def strip_prefix(runs: list[dict[str, Any]], length: int) -> list[dict[str, Any]]:
    """Remove a leading label such as ``(A)`` or ``1.``.

    ``length`` is measured on :func:`runs_to_text` of the runs, where spaces are
    collapsed, so only the non-space characters of the label are counted here.
    """

    label = runs_to_text(runs)[:length]
    needed = sum(1 for character in label if not character.isspace())
    result: list[dict[str, Any]] = []
    for run in runs:
        if needed <= 0:
            result.append(run)
            continue
        if "t" not in run or _is_special(run):
            # A label never contains a special run; stop consuming.
            needed = 0
            result.append(run)
            continue
        text = run["t"]
        cut = 0
        while cut < len(text) and needed > 0:
            if not text[cut].isspace():
                needed -= 1
            cut += 1
        rest = text[cut:]
        if rest:
            result.append({**run, "t": rest})
    return normalize_runs(result)


def _parse_table(environment: str, body: str) -> list[list[list[dict[str, Any]]]]:
    index = 0
    if environment == "tabular*":
        _args, index = _read_args(body, index, 1)
    _optional, index = _read_optional(body, index)
    if environment in {"tabularx", "tabular*"}:
        _args, index = _read_args(body, index, 1)
    _spec, index = _read_args(body, index, 1)
    content = body[index:]
    rows: list[list[list[dict[str, Any]]]] = []
    for row in _split_top_level(content, "\\\\"):
        row = re.sub(r"^\s*\[[^\]]*\]", "", row)
        row = re.sub(r"\\(?:hline|toprule|midrule|bottomrule)\b", "", row)
        row = re.sub(r"\\cline\{[^}]*\}", "", row)
        row = re.sub(r"\\arrayrulecolor\{[^}]*\}", "", row)
        if not row.strip():
            continue
        cells = []
        for cell in _split_top_level(row, "&"):
            cell = re.sub(r"\\cellcolor(?:\[[^\]]*\])?\{[^}]*\}", "", cell)
            cells.append(events_to_runs(scan_blocks(cell)))
        if any(cells):
            rows.append(cells)
    return rows


def _split_top_level(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(text):
        if text.startswith(separator, index) and depth == 0:
            parts.append(text[start:index])
            index += len(separator)
            start = index
            continue
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    parts.append(text[start:])
    return parts


def events_to_runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten block events (inside a solution box, list item or table cell) to runs."""

    runs: list[dict[str, Any]] = []
    for event in events:
        kind = event["k"]
        if runs:
            runs.append({"br": True})
        if kind == "para":
            runs.extend(event["runs"])
        elif kind == "choices":
            for position, option in enumerate(event["options"]):
                if position:
                    runs.append({"line": True})
                runs.append({"t": f"({option['label']}) "})
                runs.extend(option["runs"])
        elif kind == "list":
            for position, item in enumerate(event["items"]):
                if position:
                    runs.append({"line": True})
                runs.append({"t": "• "})
                runs.extend(item)
        elif kind == "table":
            runs.append({"table": event["rows"]})
        elif kind == "arrows":
            runs.append({"t": "↓\u2003↓\u2003↓", "g": True, "center": True})
        elif kind in {"title", "subheading", "letter", "label", "pill"}:
            runs.append({"t": event["text"][0], "b": True})
        elif kind in {"answer", "answer_plain", "answer_text"}:
            number, value = event["text"]
            shown = f"({value})" if kind == "answer" else value
            runs.append({"t": f"{number.rstrip('.')}. "})
            runs.append({"t": shown, "g": True})
        elif kind == "box":
            runs.extend(event["runs"])
        else:
            raise TexParseError(f"第 {event.get('line')} 行：此处不应出现 {kind}")
    return normalize_runs(runs)


# ---------------------------------------------------------------------------
# Passages and questions

def _topic_from_explanation(runs: list[dict[str, Any]]) -> str:
    for run in runs[:12]:
        if run.get("wave") and run.get("t"):
            name = re.sub(r"\s+", "", run["t"])
            # A wavy underline also marks key words; only a type name counts.
            topic = TOPIC_ALIASES.get(name) or TOPIC_ALIASES.get(name.rstrip("题"))
            if topic:
                return topic
    text = runs_to_text(runs)[:40]
    match = re.match(r"^这是(?:一[题道])?(.{2,8}?)(?:的题型|题|，|。)", text)
    if match:
        name = match.group(1).strip()
        for candidate in (name, name + "题"):
            if candidate in TOPIC_ALIASES:
                return TOPIC_ALIASES[candidate]
    return ""


def _answer_value(raw: str) -> tuple[str, str]:
    """``(value, note)``: ``"v [A 段]"`` -> ``("v", "[A 段]")``."""

    text = re.sub(r"\s+", " ", raw).strip()
    match = re.match(r"^(.*?)\s*([\[［（(〔][^\]］）)〕]*[段节][^\]］）)〕]*[\]］）)〕]|[\[［〔][^\]］〕]*[\]］〕])$", text)
    if match and match.group(1):
        return match.group(1).strip(), match.group(2).strip()
    return text, ""


def _letters(start: str, end: str) -> list[str]:
    return [chr(code) for code in range(ord(start), ord(end) + 1)]


class _Unit:
    """One passage (or exercise) with its questions, while assembling."""

    def __init__(self, heading: str, line: int) -> None:
        self.heading = heading
        self.line = line
        self.blocks: list[dict[str, Any]] = []        # passage as display blocks
        self.translation: list[dict[str, Any]] = []   # translation as display blocks
        self.questions: dict[int, dict[str, Any]] = {}
        self.order: list[int] = []
        self.instructions: list[dict[str, Any]] = []  # {"first","last","runs"}
        self.instructions_zh: list[dict[str, Any]] = []
        self.option_bank: list[dict[str, Any]] = []   # {"label","runs"}
        self.bank_kind = ""                           # "letters" | "roman" | "numbers"
        self.key: dict[int, str] = {}
        self.spans: dict[int, int] = {}               # "11-12": one question answering two numbers
        self.implicit = False                         # a worked example's unnumbered question
        self.solutions: dict[int, dict[str, Any]] = {}
        self.shared_explanation: list[dict[str, Any]] = []
        self.issues: list[str] = []
        self.book_part = "practice"


def _question(unit: _Unit, number: int, line: int) -> dict[str, Any]:
    if number not in unit.questions:
        unit.questions[number] = {"number": number, "stem": [], "options": [], "layout": "", "line": line}
        unit.order.append(number)
    return unit.questions[number]


_NUMBERED = re.compile(r"^(\d{1,2})\.\s*")
_LETTERED = re.compile(r"^\(?([A-Z])[.)]\s*")
_ROMAN = re.compile(r"^(x{0,1}(?:ix|iv|v?i{0,3}))\.\s*", re.I)
_RANGE = re.compile(r"(?:Questions?|第)\s*(\d+)\s*(?:[-–—~至到和与]+|and)\s*(\d+)")
_SINGLE_RANGE = re.compile(r"(?:Question|第)\s*(\d+)\s*(?:题)?$")


def _assemble_unit(unit: _Unit, events: list[dict[str, Any]]) -> None:
    """Fill a unit from its events: passage, questions, key, translation, solutions."""

    phase = "passage"
    current: dict[str, Any] | None = None
    bank_mode = ""
    instruction: dict[str, Any] | None = None
    solution: dict[str, Any] | None = None
    # Answer lines in a row with nothing between them share the next box.
    bare_markers: list[dict[str, Any]] = []
    zh_instruction: dict[str, Any] | None = None
    pending_letter = ""

    for event in events:
        kind = event["k"]
        if kind == "key":
            for number, value in _parse_answer_key(event).items():
                if number in unit.key and unit.key[number] != value:
                    unit.issues.append(f"答案表第 {number} 题出现两次且不一致")
                unit.key[number] = value
            continue
        if kind == "pill" and event["text"][0] == "答案":
            phase = "key"
            continue
        if kind == "translation" or (kind == "label" and event["text"][0] == "翻译"):
            phase = "translation"
            current = None
            continue
        if kind == "label" and event["text"][0] == "题解":
            phase = "solutions"
            solution = None
            continue
        if phase == "key":
            if kind == "para":
                for number, value in _parse_key_text(runs_to_text(event["runs"])).items():
                    unit.key[number] = value
                continue
            if kind in {"answer", "answer_plain", "answer_text"}:
                number = int(re.sub(r"\D", "", event["text"][0]) or 0)
                unit.key[number] = _key_value(kind, event["text"][1])
                continue
            if kind == "table":
                for number, value in _parse_key_text(_table_text(event["rows"])).items():
                    unit.key[number] = value
                continue
            phase = "passage"
        if phase == "translation":
            unit.translation.append(_display_block(event))
            continue
        if phase == "solutions":
            if kind in {"answer", "answer_plain", "answer_text"}:
                span = re.match(r"\s*(\d+)\s*[-–—]\s*(\d+)", event["text"][0])
                if span:
                    unit.spans[int(span.group(1))] = int(span.group(2))
                number = int(span.group(1)) if span else int(re.sub(r"\D", "", event["text"][0]) or 0)
                if not number and unit.implicit:
                    # "\ReadAnswerText{}{(D)}": the answer to the example's unnumbered question.
                    number = len(unit.solutions) + 1
                if not number:
                    unit.issues.append(f"第 {event['line']} 行：题解答案行缺少题号")
                    continue
                value = _key_value(kind, event["text"][1])
                if solution is None or solution["zh"] or solution["explanation"]:
                    bare_markers = []
                solution = unit.solutions.setdefault(number, {"answer": value, "zh": [], "explanation": [],
                                                              "line": event["line"]})
                solution["answer"] = value
                bare_markers.append(solution)
                zh_instruction = None
                continue
            if kind == "para" and re.fullmatch(r"(?:\d{1,2}\.\s*[A-Z]\s+)+\d{1,2}\.\s*[A-Z]\s*",
                                               runs_to_text(event["runs"])):
                # A worked example's answers on one line: "1. H  2. F  3. E".
                unit.key.update(_parse_key_text(runs_to_text(event["runs"])))
                continue
            if kind == "para":
                green = _green_answer(event["runs"])
                if green is not None:
                    number, value = green
                    solution = unit.solutions.setdefault(number, {"answer": value, "zh": [], "explanation": [],
                                                                  "line": event["line"]})
                    zh_instruction = None
                    continue
            if kind == "para" and re.match(r"^第\s*\d+\s*(?:[-–—~至到]+|和|与)\s*\d+\s*题", runs_to_text(event["runs"])):
                event = {"k": "subheading", "text": [runs_to_text(event["runs"])], "line": event["line"]}
                kind = "subheading"
            if kind == "subheading":
                match = _RANGE.search(event["text"][0])
                if match:
                    zh_instruction = {"first": int(match.group(1)), "last": int(match.group(2)),
                                      "runs": [{"t": event["text"][0], "b": True}]}
                    unit.instructions_zh.append(zh_instruction)
                    solution = None
                    continue
            if kind == "box":
                if len(bare_markers) > 1 and all(not item["zh"] and not item["explanation"] for item in bare_markers):
                    for item in bare_markers:
                        item["explanation"] = list(event["runs"])
                        item["shared"] = True
                    bare_markers = []
                    continue
                bare_markers = []
                if solution is None:
                    unit.shared_explanation.extend(([{"br": True}] if unit.shared_explanation else []) + event["runs"])
                else:
                    solution["explanation"].extend(([{"br": True}] if solution["explanation"] else []) + event["runs"])
                continue
            if solution is None:
                if zh_instruction is not None:
                    zh_instruction["runs"] += [{"br": True}] + _block_runs(event)
                elif unit.shared_explanation:
                    unit.shared_explanation.extend([{"br": True}] + _block_runs(event))
                else:
                    zh_instruction = {"first": 0, "last": 0, "runs": _block_runs(event)}
                    unit.instructions_zh.append(zh_instruction)
                continue
            if solution["explanation"]:
                solution.setdefault("after_box", []).append(event.get("line"))
                solution["explanation"].extend([{"br": True}] + _block_runs(event))
            else:
                solution["zh"].append(_display_block(event))
            continue
        # Passage and question region.
        if kind == "questions_start" or (kind == "label" and event["text"][0] == "例题"):
            phase = "questions"
            bank_mode = ""
            continue
        if kind == "subheading":
            title = event["text"][0]
            match = _RANGE.search(title)
            if match:
                instruction = {"first": int(match.group(1)), "last": int(match.group(2)),
                               "runs": [{"t": title, "b": True}]}
                unit.instructions.append(instruction)
                bank_mode = ""
                current = None
                phase = "questions"
                continue
            if re.match(r"(?i)^(list of|answer choices)", title):
                bank_mode = "summary" if title.lower().startswith("answer") else "list"
                unit.blocks.append(_display_block(event))
                continue
            unit.blocks.append(_display_block(event))
            continue
        if kind == "para" and phase == "passage" and re.fullmatch(r"[A-Z]", runs_to_text(event["runs"])):
            # A section letter typeset as a bold paragraph of its own.
            event = {"k": "letter", "text": [runs_to_text(event["runs"])], "line": event["line"]}
            kind = "letter"
        if kind == "letter":
            pending_letter = event["text"][0]
            unit.blocks.append(_display_block(event))
            continue
        if kind == "choices" and instruction is not None and any(
            option["label"].isdigit() and instruction["first"] <= int(option["label"]) <= instruction["last"]
            for option in event["options"]
        ):
            # A question table: "1. Section B | 4. Section E"; unlabelled cells are the worked example.
            for option in event["options"]:
                if option["label"].isdigit():
                    current = _question(unit, int(option["label"]), event["line"])
                    current["stem"] = option["runs"]
                else:
                    instruction["runs"] = instruction["runs"] + [{"line": True}] + option["runs"]
            phase = "questions"
            continue
        if kind == "choices" and any(not option["label"] for option in event["options"]):
            unit.issues.append(f"第 {event['line']} 行：选项缺少标号")
            continue
        if kind == "choices":
            if current is not None and not current["options"] and phase == "questions" and bank_mode == "":
                current["options"] = event["options"]
                current["layout"] = event["layout"]
            else:
                unit.option_bank.extend(event["options"])
                unit.bank_kind = unit.bank_kind or "letters"
                unit.blocks.append({"kind": "options", "options": event["options"]})
            continue
        if kind == "table" and current is not None and phase == "questions":
            flat: list[dict[str, Any]] = []
            for row in event["rows"]:
                for cell in row:
                    flat.extend(cell + [{"t": " "}])
            insert = split_insert_options(normalize_runs(flat))
            if insert:
                current["options"] = insert[1]
                current["layout"] = "two_column"
                continue
            cells = [cell for row in event["rows"] for cell in row if cell]
            if not current["options"] and cells and all(re.match(r"^\([A-H]\)", runs_to_text(cell)) for cell in cells):
                # Choices laid out as a two-column table: (A) | (C) / (B) | (D).
                current["options"] = [_option_from_runs(cell, event["line"]) for cell in cells]
                current["layout"] = "two_column"
                continue
        if kind == "table":
            used = _table_questions(unit, event)
            if not used:
                unit.blocks.append(_display_block(event))
            continue
        if kind == "para" and re.match(r"^Questions?\s+\d+\s*(?:[-–—]+|and)\s*\d+", runs_to_text(event["runs"])):
            match = _RANGE.search(runs_to_text(event["runs"]))
            instruction = {"first": int(match.group(1)), "last": int(match.group(2)), "runs": list(event["runs"]),
                           "options": []}
            unit.instructions.append(instruction)
            bank_mode = ""
            current = None
            phase = "questions"
            continue
        if kind == "para" and re.fullmatch(r"(?i)(list of [A-Za-z ]+|answer choices)", runs_to_text(event["runs"])):
            # A list heading typeset as a bold paragraph.
            title = runs_to_text(event["runs"])
            bank_mode = "summary" if title.lower().startswith("answer") else "list"
            unit.blocks.append({"kind": "subheading", "text": title})
            continue
        if kind == "para":
            text = runs_to_text(event["runs"])
            numbered = _NUMBERED.match(text)
            summary_choice = re.match(r"^(\d)\.?\s+", text)
            if bank_mode == "summary" and summary_choice:
                # One choice is printed without the period after its numeral ("3 Taylor ...").
                rest = strip_prefix(event["runs"], summary_choice.end())
                unit.option_bank.append({"label": summary_choice.group(1), "runs": rest})
                unit.bank_kind = "numbers"
                unit.blocks.append({"kind": "option", "label": summary_choice.group(1), "runs": rest})
                if current is not None:
                    current["options"].append({"label": summary_choice.group(1), "runs": rest})
                continue
            roman = _ROMAN.match(text)
            if bank_mode == "list" and roman and roman.group(1):
                unit.option_bank.append({"label": roman.group(1).lower(), "runs": strip_prefix(event["runs"], roman.end())})
                unit.bank_kind = "roman"
                unit.blocks.append({"kind": "option", "label": roman.group(1).lower(),
                                    "runs": strip_prefix(event["runs"], roman.end())})
                continue
            lettered = _LETTERED.match(text)
            if lettered and (bank_mode == "list" or (phase == "questions" and current is None)
                             or (phase == "passage" and re.match(r"^[A-Z]\.\s", text) and unit.bank_kind != "roman")):
                label = lettered.group(1)
                if instruction is not None and current is None and "options" in instruction:
                    instruction["options"].append({"label": label, "runs": strip_prefix(event["runs"], lettered.end())})
                unit.option_bank.append({"label": label, "runs": strip_prefix(event["runs"], lettered.end())})
                unit.bank_kind = unit.bank_kind or "letters"
                unit.blocks.append({"kind": "option", "label": label,
                                    "runs": strip_prefix(event["runs"], lettered.end())})
                continue
            if numbered and "→" not in text and (phase == "questions" or instruction is not None or unit.order):
                number = int(numbered.group(1))
                current = _question(unit, number, event["line"])
                current["stem"] = strip_prefix(event["runs"], numbered.end())
                bank_mode = ""
                phase = "questions"
                continue
            option = re.match(r"^\(([A-H])\)\s*", text)
            if current is not None and phase == "questions" and option and bank_mode == "" \
                    and not split_insert_options(event["runs"]):
                parts = split_labelled_runs(event["runs"])
                current["options"].extend(parts)
                current["layout"] = "two_column" if len(parts) > 1 else (current["layout"] or "single")
                continue
            if current is None and phase == "questions" and unit.book_part == "method" and not unit.instructions \
                    and not option and not lettered:
                # A worked example asks one question without a number.
                unit.implicit = True
                current = _question(unit, len(unit.order) + 1, event["line"])
                current["stem"] = event["runs"]
                continue
            if current is not None and phase == "questions":
                insert = split_insert_options(event["runs"])
                if insert:
                    before, current["options"] = insert
                    current["layout"] = "two_column"
                    if before:
                        current["stem"] = current["stem"] + [{"line": True}] + before
                else:
                    current["stem"] = current["stem"] + [{"line": True}] + event["runs"]
                continue
            if instruction is not None and phase == "questions":
                instruction["runs"] += [{"line": True}] + event["runs"]
                continue
            block = _display_block(event)
            if pending_letter:
                block["section"] = pending_letter
                pending_letter = ""
            unit.blocks.append(block)
            continue
        if kind == "list" and event.get("ordered") and (phase == "questions" or instruction is not None):
            for offset, item in enumerate(event["items"]):
                current = _question(unit, event["start"] + offset, event["line"])
                current["stem"] = item
            bank_mode = ""
            phase = "questions"
            continue
        if kind in {"title", "article_title2", "pill", "label", "arrows", "list", "tikz"}:
            unit.blocks.append(_display_block(event))
            continue
        if kind == "box":
            unit.shared_explanation.extend(event["runs"])
            continue
        if kind in {"answer", "answer_plain", "answer_text"}:
            number = int(re.sub(r"\D", "", event["text"][0]) or 0)
            unit.key[number] = _key_value(kind, event["text"][1])
            continue
        unit.issues.append(f"第 {event.get('line')} 行：文章区出现无法归类的 {kind}")


def split_labelled_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``(A) rocky (C) huge`` -> two options; labels must increase."""

    pieces: list[tuple[str, list[dict[str, Any]]]] = []
    label = ""
    current: list[dict[str, Any]] = []
    for run in runs:
        if "t" not in run or _is_special(run):
            current.append(run)
            continue
        text = run["t"]
        position = 0
        for match in re.finditer(r"(?:^|(?<=\s))\(([A-H])\)\s*", text):
            if label and match.group(1) <= label:
                continue
            if match.start() > position:
                current.append({**run, "t": text[position:match.start()]})
            if label:
                pieces.append((label, current))
            label, current = match.group(1), []
            position = match.end()
        if position < len(text):
            current.append({**run, "t": text[position:]})
    if label:
        pieces.append((label, current))
    return [{"label": key, "runs": normalize_runs(value)} for key, value in pieces]


def split_insert_options(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """``... (A) [A] (B) [B] (C) [C] (D) [D]``: the choices of a sentence-insertion question.

    Returns ``(text before the choices, options)`` or ``None``.
    """

    for index, run in enumerate(runs):
        if "t" not in run or _is_special(run) or index + 1 >= len(runs) or "box" not in runs[index + 1]:
            continue
        match = re.search(r"\(([A-E])\)\s*$", run["t"])
        if match is None or runs[index + 1]["box"] != match.group(1):
            continue
        tail = runs[index + 1:]
        text = re.sub(r"\s+", "", runs_to_text([{"t": "(" + match.group(1) + ")"}] + tail))
        if not re.fullmatch(r"(?:\([A-E]\)\[[A-E]\])+", text):
            return None
        boxes = [item["box"] for item in tail if "box" in item]
        if len(boxes) < 4:
            return None
        before = runs[:index] + ([{**run, "t": run["t"][:match.start()]}] if run["t"][:match.start()].strip() else [])
        return normalize_runs(before), [{"label": box, "runs": [{"box": box}]} for box in boxes]
    return None


def _table_text(rows: list[list[list[dict[str, Any]]]]) -> str:
    return "\n".join(" ¦ ".join(runs_to_text(cell) for cell in row) for row in rows)


def _table_questions(unit: _Unit, event: dict[str, Any]) -> bool:
    """A matching table: numbered cells are questions, lettered cells the choices."""

    # A list of headings typeset as a table: "i. | heading text".
    roman_rows = [
        (runs_to_text(row[0]).rstrip(".").lower(), row[1]) for row in event["rows"]
        if len(row) >= 2 and re.fullmatch(r"[ivx]+\.?", runs_to_text(row[0]), re.I)
        and runs_to_text(row[0]).rstrip(".").lower() in ROMAN
    ]
    if len(roman_rows) >= 3 and len(roman_rows) == len(event["rows"]):
        unit.option_bank.extend({"label": label, "runs": runs} for label, runs in roman_rows)
        unit.bank_kind = "roman"
        unit.blocks.append({"kind": "table", "rows": event["rows"]})
        return True
    numbered: list[tuple[int, list[dict[str, Any]]]] = []
    lettered: list[tuple[str, list[dict[str, Any]]]] = []
    lines: list[list[dict[str, Any]]] = []
    for row in event["rows"]:
        for cell in row:
            line: list[dict[str, Any]] = []
            for run in cell + [{"br": True}]:
                if run.get("br") or run.get("line"):
                    if line:
                        lines.append(normalize_runs(line))
                    line = []
                else:
                    line.append(run)
    for cell in lines:
        text = runs_to_text(cell)
        match = _NUMBERED.match(text)
        if match:
            numbered.append((int(match.group(1)), strip_prefix(cell, match.end())))
            continue
        match = re.match(r"^([A-Z])\.\s*", text)
        if match:
            lettered.append((match.group(1), strip_prefix(cell, match.end())))
    if len(numbered) < 2 and len(lettered) < 2:
        return False
    for number, runs in numbered:
        _question(unit, number, event["line"])["stem"] = runs
    for label, runs in lettered:
        unit.option_bank.append({"label": label, "runs": runs})
    if lettered:
        unit.bank_kind = unit.bank_kind or "letters"
    unit.blocks.append({"kind": "table", "rows": event["rows"]})
    return True


def _green_answer(runs: list[dict[str, Any]]) -> tuple[int, str] | None:
    """A solution paragraph that starts with a green ``1. v`` answer (Chapter 5 IELTS)."""

    if not runs or not runs[0].get("g") or "t" not in runs[0]:
        return None
    match = re.match(r"^(\d{1,2})\.\s*(\S.*)$", runs[0]["t"].strip())
    if match is None:
        return None
    rest = runs_to_text(runs[1:])
    value = match.group(2).strip()
    return int(match.group(1)), (value + (" " + rest if rest else "")).strip()


def _key_value(kind: str, raw: str) -> str:
    value = _plain_inline(raw) if "\\" in raw or "{" in raw else re.sub(r"\s+", " ", raw).strip()
    return f"({value})" if kind == "answer" else value


def _parse_answer_key(event: dict[str, Any]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for inner in event["events"]:
        kind = inner["k"]
        if kind == "para":
            answers.update(_parse_key_text(runs_to_text(inner["runs"])))
        elif kind in {"answer", "answer_plain", "answer_text"}:
            number = int(re.sub(r"\D", "", inner["text"][0]) or 0)
            answers[number] = _key_value(kind, inner["text"][1])
        elif kind == "table":
            answers.update(_parse_key_text(_table_text(inner["rows"])))
        elif kind == "pill":
            continue
        else:
            raise TexParseError(f"第 {inner.get('line')} 行：答案表里出现了 {kind}")
    return answers


def _parse_key_text(text: str) -> dict[int, str]:
    """``1. (B)  2. (C)`` / table cells ``1. v`` / ``13. 2, 3, 5`` -> {number: value}."""

    answers: dict[int, str] = {}
    pieces = re.split(r"\s*[¦\n\u2003]\s*", text)
    for piece in pieces:
        for match in re.finditer(r"(\d{1,2})\.\s*(.+?)(?=\s+\d{1,2}\.\s|$)", piece.strip()):
            answers[int(match.group(1))] = re.sub(r"\s+", " ", match.group(2)).strip()
    return answers


def _display_block(event: dict[str, Any]) -> dict[str, Any]:
    kind = event["k"]
    if kind == "para":
        return {"kind": "para", "runs": event["runs"]}
    if kind in {"title", "subheading", "pill", "label", "letter", "chapter_title", "group_heading",
                "standalone_heading"}:
        return {"kind": kind, "text": event["text"][0]}
    if kind == "article_title2":
        return {"kind": "title", "text": " ".join(part for part in event["text"] if part)}
    if kind == "choices":
        return {"kind": "options", "options": event["options"], "layout": event["layout"]}
    if kind == "list":
        return {"kind": "list", "items": event["items"], "ordered": event.get("ordered", False)}
    if kind == "table":
        return {"kind": "table", "rows": event["rows"]}
    if kind == "arrows":
        return {"kind": "arrows"}
    if kind == "box":
        return {"kind": "box", "runs": event["runs"]}
    if kind in {"answer", "answer_plain", "answer_text"}:
        return {"kind": "answer", "number": event["text"][0].rstrip("."), "value": _key_value(kind, event["args"][1])}
    if kind == "tikz":
        return {"kind": "tikz"}
    if kind == "translation":
        return {"kind": "label", "text": "翻译"}
    if kind == "questions_start":
        return {"kind": "spacer"}
    if kind in {"type_heading", "variant_heading"}:
        return {"kind": "heading", "text": event["text"][-1]}
    return {"kind": "unknown", "event": kind}


def _block_runs(event: dict[str, Any]) -> list[dict[str, Any]]:
    if event["k"] in {"title", "subheading", "letter", "label", "pill"}:
        return [{"t": event["text"][0], "b": True}]
    return events_to_runs([event])


def blocks_to_runs(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display blocks as one run list (for a question's Chinese text)."""

    runs: list[dict[str, Any]] = []
    for block in blocks:
        if runs:
            runs.append({"br": True})
        kind = block["kind"]
        if kind == "para" or kind == "box":
            runs.extend(block["runs"])
        elif kind == "options":
            two = block.get("layout") == "two_column"
            for position, option in enumerate(block["options"]):
                if position:
                    runs.append({"t": "\u2003\u2003"} if two and position % 2 else {"line": True})
                runs.append({"t": f"({option['label']}) "})
                runs.extend(option["runs"])
        elif kind == "option":
            runs.append({"t": f"{block['label']}. ", "b": True})
            runs.extend(block["runs"])
        elif kind == "list":
            for position, item in enumerate(block["items"]):
                if position:
                    runs.append({"line": True})
                runs.append({"t": "• "})
                runs.extend(item)
        elif kind == "table":
            runs.append({"table": block["rows"]})
        elif kind in {"title", "subheading", "pill", "label", "letter", "heading"}:
            runs.append({"t": block["text"], "b": True})
        elif kind == "arrows":
            runs.append({"t": "↓\u2003↓\u2003↓", "g": True})
    return normalize_runs(runs)


# ---------------------------------------------------------------------------
# Building questions from a unit

def _question_type(unit: _Unit, question: dict[str, Any], answer: str) -> tuple[str, dict[str, Any]]:
    """Question type and answer key (see ``record_question_attempt``)."""

    value, _note = _answer_value(answer)
    # \ReadAnswerLine prints its value in brackets: "(B)", "(1,4,5)".
    bracketed = re.fullmatch(r"\(([A-Z]|\d(?:\s*[,，、]\s*\d)+)\)", value.strip())
    bare = bracketed.group(1) if bracketed else value.strip()
    upper = bare.upper()
    if upper in TFNG_VALUES:
        choices = ["YES", "NO", "NOT GIVEN"] if upper in {"YES", "NO"} or _instruction_mentions(unit, question, "YES") \
            else ["TRUE", "FALSE", "NOT GIVEN"]
        return "true_false_ng", {"kind": "label", "value": upper, "choices": choices}
    if re.fullmatch(r"[ivx]+", bare) and bare in ROMAN:
        labels = [option["label"] for option in unit.option_bank if option["label"] in ROMAN]
        return "heading_match", {"kind": "label", "value": bare, "choices": labels}
    if re.fullmatch(r"[A-Z](?:\s*[,，、]\s*[A-Z])+", bare):
        values = sorted(re.findall(r"[A-Z]", bare))
        labels = [option["label"] for option in question["options"]] or _gap_choices(unit, question)
        return "multi_select", {"kind": "set", "value": values, "choices": labels}
    if re.fullmatch(r"\d(?:\s*[,，、]\s*\d)+", bare):
        values = sorted(re.findall(r"\d", bare))
        labels = [option["label"] for option in question["options"] if option["label"].isdigit()] or [
            option["label"] for option in unit.option_bank if option["label"].isdigit()
        ] or [str(i) for i in range(1, 7)]
        return "multi_select", {"kind": "set", "value": values, "choices": labels}
    if re.fullmatch(r"[A-Z]", bare):
        own = [option["label"] for option in question["options"]]
        if own:
            return "reading_choice", {"kind": "label", "value": bare, "choices": own}
        return "gap_select", {"kind": "label", "value": bare, "choices": _gap_choices(unit, question)}
    return "word_fill", {"kind": "text", "value": accepted_answers(bare)}


def accepted_answers(answer: str) -> list[str]:
    """``"(the) Outback"`` -> ``["the Outback", "Outback"]``; ``a / b`` gives both."""

    accepted: list[str] = []
    for part in re.split(r"\s*/\s*|\s+or\s+|\s*；\s*|\s*;\s*", answer):
        part = part.strip()
        if not part:
            continue
        variants = [""]
        for piece in re.split(r"(\([^()]*\))", part):
            if piece.startswith("(") and piece.endswith(")"):
                variants = [f"{value} {piece[1:-1]}" for value in variants] + variants
            else:
                variants = [value + piece for value in variants]
        for value in variants:
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in accepted:
                accepted.append(value)
    return accepted or [answer]


def _letter_range(text: str) -> list[str]:
    match = re.search(r"\b([A-Z])\s*[-–—]+\s*([A-Z])\b", text)
    if match and match.group(1) < match.group(2):
        return _letters(match.group(1), match.group(2))
    return []


def _gap_choices(unit: _Unit, question: dict[str, Any]) -> list[str]:
    """The letters a gap or matching question chooses from."""

    for instruction in unit.instructions:
        if instruction["first"] <= question["number"] <= instruction["last"]:
            letters = _letter_range(runs_to_text(instruction["runs"]))
            if letters:
                return letters
    sections = [block["text"] for block in unit.blocks
                if block["kind"] == "letter" and re.fullmatch(r"[A-Z]", block.get("text") or "")]
    bank = [option["label"] for option in unit.option_bank if re.fullmatch(r"[A-Z]", option["label"])]
    if "查找出处" in unit.heading and sections:
        return sorted(dict.fromkeys(sections))
    return sorted(dict.fromkeys(bank or sections))


def _instruction_mentions(unit: _Unit, question: dict[str, Any], word: str) -> bool:
    for instruction in unit.instructions:
        if instruction["first"] <= question["number"] <= instruction["last"]:
            return word in runs_to_text(instruction["runs"])
    return False


def _instruction_for(items: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
    for instruction in items:
        if instruction["first"] and instruction["first"] <= number <= instruction["last"]:
            return instruction["runs"]
    return []


def unit_questions(unit: _Unit) -> list[dict[str, Any]]:
    """Pair questions with answers and explanations; report problems as issues."""

    numbers = sorted(set(unit.order) | set(unit.solutions) | set(unit.key))
    numbers = [number for number in numbers if number]
    results: list[dict[str, Any]] = []
    if not numbers:
        unit.issues.append(f"“{unit.heading}”（第 {unit.line} 行）没有找到题目")
        return results
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        unit.issues.append(f"“{unit.heading}”题号不连续：{numbers}")
    covered = {number for first, last in unit.spans.items() for number in range(first + 1, last + 1)}
    for number in numbers:
        if number in covered:
            continue
        question = unit.questions.get(number) or {"number": number, "stem": [], "options": [], "layout": "",
                                                   "line": unit.line}
        if number in unit.spans:
            # "Choose TWO letters" (Questions 11 and 12): one question for both numbers.
            instruction = next((item for item in unit.instructions
                                if item["first"] <= number <= item["last"] and item.get("options")), None)
            if instruction is not None:
                question = {**question, "stem": instruction["runs"], "options": instruction["options"],
                            "layout": "single"}
        solution = unit.solutions.get(number) or {}
        answer = str(solution.get("answer") or unit.key.get(number) or "")
        if not answer:
            unit.issues.append(f"“{unit.heading}”第 {number} 题没有答案")
            continue
        if number in unit.key and solution.get("answer"):
            if _normalise_answer(unit.key[number]) != _normalise_answer(solution["answer"]):
                unit.issues.append(
                    f"“{unit.heading}”第 {number} 题答案表（{unit.key[number]}）与题解（{solution['answer']}）不一致"
                )
        question["options"] = sorted(question["options"], key=lambda option: label_order(option["label"]))
        explanation = list(solution.get("explanation") or [])
        shared = bool(solution.get("shared"))
        if not explanation and unit.shared_explanation:
            explanation, shared = list(unit.shared_explanation), True
        question_type, answer_key = _question_type(unit, question, answer)
        if question_type == "multi_select" and not question["options"]:
            # The TOEFL summary's answer choices were typeset apart from question 13.
            question = {**question, "options": [option for option in unit.option_bank
                                                if option["label"] in answer_key["choices"]]}
        value, note = _answer_value(answer)
        zh_blocks = list(solution.get("zh") or [])
        results.append({
            "number": number,
            "number_label": f"{number}–{unit.spans[number]}" if number in unit.spans else str(number),
            "stem": question["stem"],
            "options": question["options"],
            "option_layout": question.get("layout") or "",
            "instruction": _instruction_for(unit.instructions, number),
            "instruction_zh": _instruction_for(unit.instructions_zh, number),
            "answer": value,
            "answer_note": note,
            "answer_key": answer_key,
            "answer_label": answer_key["value"] if question_type == "reading_choice" else "",
            "question_type": question_type,
            "zh": blocks_to_runs(zh_blocks),
            "explanation": normalize_runs(explanation),
            "explanation_shared": shared,
            "topic": _topic_from_explanation(explanation) or TYPE_DEFAULT_TOPICS.get(question_type, "") or (
                "复选主题" if question_type == "multi_select" and answer_key["choices"][:1] == ["1"] else ""
            ),
            "line": question.get("line") or solution.get("line") or unit.line,
        })
    return results


def _normalise_answer(value: str) -> str:
    bare, _note = _answer_value(value)
    return re.sub(r"[\s()（）,，、]", "", bare).upper()


# ---------------------------------------------------------------------------
# Part 2

def _chapter_files(chapters_dir: Path, chapter: int) -> list[tuple[str, Path]]:
    """The part files of one chapter in book order (from part2-chapter-NN.tex)."""

    main = chapters_dir / f"part2-chapter-{chapter:02d}.tex"
    text = strip_comments(main.read_text(encoding="utf-8"))
    files: list[tuple[str, Path]] = []
    for match in re.finditer(r"\\input\{chapters/(part2-chapter-\d\d-([a-z0-9]+))\}", text):
        files.append((match.group(2), chapters_dir / f"{match.group(1)}.tex"))
    return files


def _split_units(events: list[dict[str, Any]], file_kind: str) -> list[tuple[_Unit, list[dict[str, Any]]]]:
    units: list[tuple[_Unit, list[dict[str, Any]]]] = []
    current: tuple[_Unit, list[dict[str, Any]]] | None = None
    uses_unit_headings = any(event["k"] == "unit" for event in events)
    for event in events:
        if event["k"] == "unit" or (event["k"] == "section" and not uses_unit_headings):
            heading = event["text"][0]
            current = (_Unit(heading, event["line"]), [])
            units.append(current)
            continue
        if event["k"] == "section":
            continue
        if current is None:
            current = (_Unit("", event["line"]), [])
            units.append(current)
        current[1].append(event)
    return [unit for unit in units if unit[1]]


def parse_practice_tests(book_root: Path | str) -> dict[str, Any]:
    """Every passage and question of Part 2.

    Returns ``{"groups": [...], "questions": [...], "issues": [...], "warnings": [...], "counts": {...}}``.
    """

    root = Path(book_root)
    chapters_dir = root / "chapters"
    if not chapters_dir.is_dir():
        raise FileNotFoundError(f"未找到《旋元佑英文阅读》章节目录：{chapters_dir}")
    groups: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    chapter_numbers = sorted(
        int(match.group(1)) for path in chapters_dir.glob("part2-chapter-*.tex")
        if (match := re.fullmatch(r"part2-chapter-(\d\d)\.tex", path.name))
    )
    for chapter in chapter_numbers:
        numbering: dict[str, int] = {}
        for file_kind, source_file in _chapter_files(chapters_dir, chapter):
            category = FILE_KINDS.get(file_kind)
            if category is None:
                issues.append(f"{source_file.name}：未知的测验类别 {file_kind}")
                continue
            text = strip_comments(source_file.read_text(encoding="utf-8"))
            try:
                events = scan_blocks(text)
            except TexParseError as error:
                issues.append(f"{source_file.name}：{error}")
                continue
            for unit, unit_events in _split_units(events, file_kind):
                numbering[category] = numbering.get(category, 0) + 1
                prefix, category_title = READING_CATEGORIES[category]
                group_code = f"{QUESTION_CODE_PREFIX}-{chapter:02d}-{prefix}-{numbering[category]}"
                try:
                    _assemble_unit(unit, unit_events)
                    built = unit_questions(unit)
                except TexParseError as error:
                    issues.append(f"{source_file.name}（{group_code}）：{error}")
                    continue
                issues.extend(f"{source_file.name}（{group_code}）：{problem}" for problem in unit.issues)
                residues = text_residues([unit.blocks, unit.translation, unit.option_bank, unit.instructions])
                if residues:
                    issues.append(f"{source_file.name}（{group_code}）：文章或译文残留 TeX 片段：" + " ".join(residues))
                heading = unit.heading or category_title
                group = {
                    "group_code": group_code,
                    "chapter_number": chapter,
                    "reading_category": category,
                    "category_title": category_title,
                    "article_number": numbering[category],
                    "heading": heading,
                    "title": _group_title(unit, chapter, category_title, heading),
                    "blocks": unit.blocks,
                    "translation": unit.translation,
                    "option_bank": unit.option_bank,
                    "instructions": unit.instructions,
                    "instructions_zh": unit.instructions_zh,
                    "source_file": source_file.name,
                    "source_line": unit.line,
                    "book_part": "practice",
                }
                groups.append(group)
                bucket = counts.setdefault(f"{chapter:02d}-{prefix}", {"groups": 0, "questions": 0})
                bucket["groups"] += 1
                for item in built:
                    item.update({
                        "question_code": f"{group_code}-{item['number']:02d}",
                        "group_code": group_code,
                        "chapter_number": chapter,
                        "reading_category": category,
                        "category_title": category_title,
                        "heading": heading,
                        "source_file": source_file.name,
                        "book_part": "practice",
                    })
                    if not item["topic"]:
                        item["topic"] = _fallback_topic(item, heading)
                    item["warnings"] = _question_warnings(item)
                    warnings.extend(f"{item['question_code']}：{warning}" for warning in item["warnings"])
                    issues.extend(f"{item['question_code']}：{problem}" for problem in validate_question(item))
                    questions.append(item)
                    bucket["questions"] += 1
    codes = [item["question_code"] for item in questions]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        issues.append("题号重复：" + "、".join(duplicates))
    return {"groups": groups, "questions": questions, "issues": issues, "warnings": warnings, "counts": counts}


def _group_title(unit: _Unit, chapter: int, category_title: str, heading: str) -> str:
    article = next((block["text"] for block in unit.blocks if block["kind"] == "title"), "")
    parts = [f"Chapter {chapter}", category_title]
    if heading and heading != category_title:
        parts.append(heading)
    if article:
        parts.append(article)
    return " · ".join(parts)


def _fallback_topic(item: dict[str, Any], heading: str) -> str:
    """The type of a question whose explanation does not name it: the special test's
    heading (选句填空 ...), or for IELTS letter questions the kind of matching asked."""

    if heading in TOPIC_ALIASES:
        return TOPIC_ALIASES[heading]
    if item["question_type"] == "gap_select":
        instruction = runs_to_text(item["instruction"]).lower()
        if re.search(r"which (paragraph|section) contains|contains the following information", instruction):
            return "查找出处"
        return "配对"
    return ""


def _question_warnings(item: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not item["zh"] and item["book_part"] == "practice" and item["question_type"] == "reading_choice":
        warnings.append("原书题解没有题干中文对照")
    if item["explanation_shared"]:
        warnings.append("原书只给了整组共用的解析")
    if not item["topic"]:
        warnings.append("原书解析没有标出题型")
    return warnings


_RESIDUE = re.compile(
    r"\\|[{}]|ReadGreen|linewidth|baselineskip|\[(?:-?[\d.]+\s*(?:em|ex|pt|cm|mm)|[lcr])\]"
)


def text_residues(value: Any) -> list[str]:
    """TeX left in any text of a block, run list or question (empty when clean)."""

    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"t", "text"} and isinstance(child, str):
                    found.extend(match.group(0) for match in _RESIDUE.finditer(child))
                elif isinstance(child, (list, dict)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return sorted(set(found))


def validate_question(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    kind = item["question_type"]
    key = item["answer_key"]
    if kind == "reading_choice":
        labels = [option["label"] for option in item["options"]]
        if len(labels) < 3 or labels != [chr(ord("A") + i) for i in range(len(labels))]:
            errors.append(f"选项标号不连续：{labels}")
        if key["value"] not in labels:
            errors.append(f"答案 {item['answer']} 不在选项中")
        if not item["stem"]:
            errors.append("题干为空")
    elif kind in {"gap_select", "heading_match", "true_false_ng"}:
        if key["value"] not in key["choices"]:
            errors.append(f"答案 {key['value']} 不在可选范围 {key['choices']} 中")
    elif kind == "multi_select":
        if not item.get("options"):
            errors.append("复选题没有选项")
        if not set(key["value"]) <= set(key["choices"]):
            errors.append(f"答案 {key['value']} 不在可选范围 {key['choices']} 中")
    if not item["explanation"]:
        errors.append("没有解析")
    residues = text_residues([item["stem"], item["zh"], item["explanation"], item["instruction"],
                              item["options"], {"t": item["answer"]}])
    if residues:
        errors.append("残留 TeX 片段：" + " ".join(residues))
    return errors


# ---------------------------------------------------------------------------
# Part 1: the methodology

PART1_FILES = (
    "part1-overview", "part1-mainstream-intro", "part1-high-frequency", "part1-01-topic", "part1-02-detail",
    "part1-02-detail-variant", "part1-03-inference", "part1-03-inference-variant", "part1-04-vocabulary",
    "part1-05-tone", "part1-06-rhetorical-purpose", "part1-next-high-frequency", "part1-07-organization",
    "part1-08-insert-sentence", "part1-09-sentence-simplification", "part1-10-reference", "part1-special",
    "part1-toefl", "part1-ielts",
)
_SECTION_HEADINGS = {"chapter_title", "group_heading", "type_heading", "variant_heading", "standalone_heading"}


def _part1_order(chapters_dir: Path) -> list[str]:
    main = chapters_dir.parent / f"{BOOK_TITLE}.tex"
    if main.is_file():
        text = strip_comments(main.read_text(encoding="utf-8"))
        names = re.findall(r"\\input\{chapters/(part1-[a-z0-9-]+)\}", text)
        if names:
            return names
    return list(PART1_FILES)


def parse_method(book_root: Path | str) -> dict[str, Any]:
    """Part 1 as sections (lecture blocks + worked examples) and example questions."""

    root = Path(book_root)
    chapters_dir = root / "chapters"
    sections: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    issues: list[str] = []
    warnings: list[str] = []
    parent_titles: dict[int, str] = {}
    for stem in _part1_order(chapters_dir):
        source_file = chapters_dir / f"{stem}.tex"
        try:
            events = scan_blocks(strip_comments(source_file.read_text(encoding="utf-8")))
        except (OSError, TexParseError) as error:
            issues.append(f"{source_file.name}：{error}")
            continue
        file_sections: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for event in events:
            if event["k"] in _SECTION_HEADINGS:
                level, title, number = _heading_info(event)
                section = {"title": title, "number": number, "level": level, "source_file": source_file.name,
                           "source_line": event["line"]}
                file_sections.append((section, []))
                continue
            if not file_sections:
                # The overview has no heading of its own.
                file_sections.append(({"title": "方法论总览", "number": "", "level": 1,
                                       "source_file": source_file.name, "source_line": event["line"]}, []))
            file_sections[-1][1].append(event)
        for position, (section, section_events) in enumerate(file_sections):
            key_base = stem.replace("part1-", "")
            section["key"] = f"{QUESTION_CODE_PREFIX}-M-{key_base}" + (f"-{position + 1}" if len(file_sections) > 1 else "")
            # The parent is the nearest heading of a higher level since the last one at this level.
            for deeper in [level for level in parent_titles if level >= section["level"]]:
                parent_titles.pop(deeper)
            higher = [level for level in parent_titles if level < section["level"]]
            section["parent"] = parent_titles[max(higher)] if higher else ""
            parent_titles[section["level"]] = section["title"]
            try:
                built = _method_section(section, section_events)
            except TexParseError as error:
                issues.append(f"{source_file.name}（{section['title']}）：{error}")
                continue
            residues = text_residues([built["section"]["blocks"], [group["blocks"] for group in built["groups"]],
                                      [group["translation"] for group in built["groups"]]])
            if residues:
                issues.append(f"{source_file.name}（{section['title']}）：残留 TeX 片段：" + " ".join(residues))
            sections.append(built["section"])
            groups.extend(built["groups"])
            questions.extend(built["questions"])
            issues.extend(f"{source_file.name}（{section['title']}）：{problem}" for problem in built["issues"])
            warnings.extend(f"{item['question_code']}：{warning}" for item in built["questions"]
                            for warning in item["warnings"])
    return {"sections": sections, "groups": groups, "questions": questions, "issues": issues, "warnings": warnings}


def _heading_info(event: dict[str, Any]) -> tuple[int, str, str]:
    kind = event["k"]
    texts = event["text"]
    if kind == "chapter_title":
        return 1, texts[0], ""
    if kind == "group_heading":
        return 2, texts[0], ""
    if kind == "type_heading":
        prefix, number, title = texts
        title = title.lstrip("：:")
        return 3, (f"{prefix} {number}：{title}" if prefix else title), number
    if kind == "variant_heading":
        number, title = texts
        return 4, title, number
    return 3, texts[0], ""


def _method_section(section: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Split a section into lecture blocks and worked examples (each example a unit)."""

    blocks: list[dict[str, Any]] = []
    examples: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    anchors: dict[str, str] = {}
    for event in events:
        starts_example = event["k"] == "pill" and event["text"][0] == "范例"
        if event["k"] == "label" and event["text"][0] == "例题" and current is None:
            # An exercise on the previous section's passage starts at 例题.
            starts_example = True
        if starts_example:
            current = [event] if event["k"] == "label" else []
            examples.append(current)
            blocks.append({"kind": "example", "index": len(examples) - 1})
            continue
        for anchor in event.get("anchors") or []:
            anchors[anchor] = _squash(runs_to_text(event.get("runs") or []))[:30]
        if current is not None:
            current.append(event)
            continue
        if event["k"] == "tikz":
            blocks.append({"kind": "overview"})
            continue
        blocks.append(_display_block(event))
    groups: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, example_events in enumerate(examples):
        group_code = f"{section['key']}-{index + 1}"
        unit = _Unit(f"{section['title']} 范例", example_events[0]["line"] if example_events else section["source_line"])
        unit.book_part = "method"
        _assemble_unit(unit, example_events)
        has_question = bool(unit.questions or unit.solutions or unit.key)
        built = unit_questions(unit) if has_question else []
        if has_question:
            issues.extend(unit.issues)
        group = {
            "group_code": group_code,
            "chapter_number": None,
            "reading_category": "method",
            "category_title": "方法论",
            "article_number": index + 1,
            "heading": section["title"],
            "title": f"方法论 · {section['title']} · 范例" + (f" {index + 1}" if len(examples) > 1 else ""),
            "blocks": unit.blocks,
            "translation": unit.translation,
            "option_bank": unit.option_bank,
            "instructions": unit.instructions,
            "instructions_zh": unit.instructions_zh,
            "source_file": section["source_file"],
            "source_line": unit.line,
            "book_part": "method",
            "section_key": section["key"],
            # A worked example without its own question (an illustration only).
            "explanation": unit.shared_explanation if not built else [],
        }
        groups.append(group)
        for item in built:
            item.update({
                "question_code": f"{group_code}-{item['number']:02d}",
                "group_code": group_code,
                "chapter_number": None,
                "reading_category": "method",
                "category_title": "方法论",
                "heading": section["title"],
                "source_file": section["source_file"],
                "book_part": "method",
                "section_key": section["key"],
            })
            if not item["topic"]:
                item["topic"] = TOPIC_ALIASES.get(re.sub(r"^.*：", "", section["title"]).strip(), "")
            item["warnings"] = [warning for warning in _question_warnings(item) if "中文对照" not in warning]
            issues.extend(f"{item['question_code']}：{problem}" for problem in validate_question(item))
            questions.append(item)
    section = {**section, "blocks": blocks, "anchors": anchors,
               "example_codes": [group["group_code"] for group in groups],
               "question_codes": [item["question_code"] for item in questions]}
    return {"section": section, "groups": groups, "questions": questions, "issues": issues}


# ---------------------------------------------------------------------------
# Pages in the compiled PDF

def _squash(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(character for character in text.casefold() if character.isalnum())


def locate_pages(groups: list[dict[str, Any]], questions: list[dict[str, Any]], pdf_path: Path | str,
                 *, first_page: int = 1, pages: list[str] | None = None) -> dict[str, int]:
    """Find the PDF page of each passage and question.

    Passages are searched by their first sentences, forward from the previous
    passage; a question is searched from its passage's page by its stem (or
    the Chinese text of its solution).  Pages stay monotonic.  A question that
    cannot be found takes its passage's page (``inferred``).
    """

    if pages is None:
        import fitz

        with fitz.open(str(pdf_path)) as document:
            pages = [_squash(page.get_text()) for page in document]
    counts = {"located": 0, "inferred": 0, "missing": 0}
    last = max(0, int(first_page) - 1)
    group_pages: dict[str, int] = {}
    for group in groups:
        keys = []
        for block in group["blocks"]:
            if block["kind"] in {"para", "option"}:
                text = _squash(runs_to_text(block["runs"]))
                if len(text) >= 24:
                    keys.extend((text[:40], text[12:52]))
            if len(keys) >= 4:
                break
        found = _search(pages, keys, last)
        if found is not None:
            last = found
            group_pages[group["group_code"]] = found + 1
            group["source_page"] = found + 1
        else:
            group["source_page"] = None
    by_group: dict[str, list[dict[str, Any]]] = {}
    for item in questions:
        by_group.setdefault(item["group_code"], []).append(item)
    for group in groups:
        start = (group.get("source_page") or 1) - 1
        for item in by_group.get(group["group_code"], []):
            stem = _squash(runs_to_text(item["stem"]))
            zh = _squash(runs_to_text(item["zh"]))
            keys = [key for key in (stem[:40], stem[10:50]) if len(key) >= 12]
            found = _search(pages, keys, start, window=6)
            if found is None:
                zh_keys = [key for key in (zh[:24],) if len(key) >= 10]
                found = _search(pages, zh_keys, start, window=10)
                if found is not None:
                    found = None  # the Chinese text sits in the solutions; keep the question's own page
            if found is not None:
                item["source_page"] = found + 1
                item["page_status"] = "located"
                start = found
            elif group.get("source_page"):
                item["source_page"] = group["source_page"]
                item["page_status"] = "inferred"
            else:
                item["source_page"] = None
                item["page_status"] = "missing"
            counts[item["page_status"]] += 1
    return counts


def _normal_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = re.sub(r"^\s*[\d.]+\s*", "", text)
    return re.sub(r"[\s:：]+", "", text)


def locate_method(method: dict[str, Any], pdf_path: Path | str) -> dict[str, int]:
    """Pages of the Part 1 sections (from the PDF bookmarks), examples and questions,
    and the printed page numbers of ``\\pageref`` cross references."""

    import fitz

    with fitz.open(str(pdf_path)) as document:
        toc = document.get_toc()
        labels = [document[index].get_label() or str(index + 1) for index in range(document.page_count)]
        squashed = [_squash(page.get_text()) for page in document]
    part1 = next((page for level, title, page in toc if level == 1 and "方法论" in title), 1)
    part2 = next((page for level, title, page in toc if level == 1 and "阅读测验" in title and page > part1),
                 len(labels) + 1)
    entries = [(level, _normal_title(title), page) for level, title, page in toc if part1 <= page < part2]
    last = part1
    for section in method["sections"]:
        wanted = _normal_title(section["title"])
        page = next((entry_page for level, title, entry_page in entries
                     if entry_page >= last and level > 1 and (title == wanted or title.endswith(wanted))), None)
        if section["title"] == "方法论总览":
            page = part1
        section["source_page"] = page or last
        section["page_status"] = "bookmark" if page else "inferred"
        last = section["source_page"]
    # Examples and their questions, searched from the section's page.
    by_section = {section["key"]: section for section in method["sections"]}
    counts = {"located": 0, "inferred": 0, "missing": 0}
    for group in method["groups"]:
        section = by_section.get(group.get("section_key") or "")
        start = (section or {}).get("source_page") or part1
        group_counts = locate_pages([group], [item for item in method["questions"]
                                              if item["group_code"] == group["group_code"]],
                                    pdf_path, first_page=start, pages=squashed)
        if not group.get("source_page"):
            group["source_page"] = start
        for item in method["questions"]:
            if item["group_code"] == group["group_code"] and item.get("page_status") == "missing":
                item["source_page"], item["page_status"] = start, "inferred"
                group_counts["missing"] -= 1
                group_counts["inferred"] += 1
        for key, value in group_counts.items():
            counts[key] += value
    # \pageref: the printed page number of the page holding the anchored paragraph.
    anchors: dict[str, str] = {}
    for section in method["sections"]:
        anchors.update(section.get("anchors") or {})
    unresolved: list[str] = []

    def resolve(runs: Any) -> None:
        for run in runs or []:
            if "pageref" in run:
                key = anchors.get(run["pageref"], "")
                found = _search(squashed, [key], part1 - 1) if key else None
                if found is None:
                    unresolved.append(run["pageref"])
                else:
                    run["page"] = labels[found]
            if "table" in run:
                for row in run["table"]:
                    for cell in row:
                        resolve(cell)

    for section in method["sections"]:
        for block in section["blocks"]:
            resolve(block.get("runs"))
            for item in block.get("items") or []:
                resolve(item)
    for item in method["questions"]:
        for field in ("stem", "zh", "explanation", "instruction"):
            resolve(item.get(field))
    method["unresolved_pagerefs"] = unresolved
    return counts


def _search(pages: list[str], keys: list[str], start: int, *, window: int = 0) -> int | None:
    ranges = [range(start, min(len(pages), start + window))] if window else [
        range(start, min(len(pages), start + 12)), range(start, len(pages))
    ]
    for scope in ranges:
        for key in keys:
            if not key:
                continue
            found = next((index for index in scope if key in pages[index]), None)
            if found is not None:
                return found
    return None


__all__ = [
    "BOOK_TITLE",
    "PDF_NAME",
    "QUESTION_CODE_PREFIX",
    "READING_CATEGORIES",
    "TYPE_NAMES",
    "TexParseError",
    "blocks_to_runs",
    "locate_method",
    "locate_pages",
    "normalize_runs",
    "parse_inline",
    "parse_method",
    "parse_practice_tests",
    "runs_to_text",
    "scan_blocks",
    "validate_question",
]
