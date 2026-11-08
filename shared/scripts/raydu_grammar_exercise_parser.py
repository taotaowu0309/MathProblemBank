"""Structured parser for the chapter TeX of 《旋元佑文法解题》.

The book source follows the fixed layout described in the book project's
``WRITING_SYSTEM.md``: a numbered heading with ``\\sourcebox{学校 | 题型}``,
the stem (A–E ``\\uline`` segments for error questions, ``\\blank{}`` plus an
option table for choice questions), the answer line, the corrected sentence,
the Chinese translation and a ``solutionbox`` explanation.  Exam points come
from the ``\\importantpoint{...}`` that precedes each group of questions.

The parser is deliberately strict.  It understands only the macros the book
uses inside questions; anything else is reported as an issue so that a source
change can never silently leak TeX into the question bank.

Rich text is stored as a list of *runs*: ``{"t": text}`` plus optional style
keys ``u`` (underline), ``em`` (book highlight), ``i`` (italic), ``sup``,
``syn`` (sentence-element role, e.g. ``"s"``), or the special runs
``{"blank": True}`` and ``{"br": True}`` (paragraph break).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


QUESTION_CODE_PREFIX = "XYGE"

HEADING_PATTERN = re.compile(
    r"\\par\\medskip\\noindent\{\\color\{outermarginfgcolor\}\\bfseries\s*(\d+)\.\\quad\s*"
    r"\\sourcebox\{([^{}]*)\}\}"
)
ANSWER_MARKER = re.compile(r"\{\\color\{outermarginfgcolor\}\\bfseries\s*答案\\quad")
TRANSLATION_MARKER = re.compile(r"\\par\\smallskip\{\\color\{black!75\}")
SOLUTION_PATTERN = re.compile(
    r"\\begin\{solutionbox\}\s*\\noindent\\solutionmark\\enspace(.*?)\\end\{solutionbox\}",
    re.S,
)
BOUNDARY_PATTERN = re.compile(r"\\importantpoint\{|\\section\*?\{|\\chapter\*?\{")
CHAPTER_PATTERN = re.compile(r"\\chapter\{([^{}]*)\}")
LABEL_ULINE_PATTERN = re.compile(r"\(([A-E])\)\s*\\uline\{")

TYPE_LABELS = {"挑错题": "error_choice", "改错题": "correction"}
TYPE_NAMES = {"choice": "选择题", "error_choice": "挑错题", "correction": "改错题"}

_IGNORED_COMMANDS = {
    "noindent", "smallskip", "medskip", "bigskip", "normalfont", "scriptsize",
    "footnotesize", "small", "allowbreak", "centering", "clearpage", "newpage",
    "relax", "solutionmark",
}
_SPACE_COMMANDS = {"quad", "qquad", "enspace", "thinspace"}
_SYMBOL_COMMANDS = {
    "rightarrow": "→", "leftarrow": "←", "textbullet": "•", "textasciicircum": "^",
    "ldots": "…", "dots": "…", "times": "×", "S": "§",
}
_STYLE_COMMANDS = {
    "uline": "u", "rdcolor": "em", "textbf": "em", "textit": "i", "emph": "i",
    "textsuperscript": "sup",
}
_ESCAPED_CHARACTERS = {"%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", " ": " ", ",": " "}


class TexParseError(ValueError):
    pass


def _read_group(text: str, start: int) -> tuple[str, int]:
    """Return the content of the ``{...}`` group opening at ``start`` and the index after it."""

    if start >= len(text) or text[start] != "{":
        raise TexParseError("缺少花括号参数")
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


def _skip_optional(text: str, index: int) -> int:
    if index < len(text) and text[index] == "[":
        end = text.find("]", index)
        if end < 0:
            raise TexParseError("方括号不成对")
        return end + 1
    return index


def parse_inline(source: str) -> list[dict[str, Any]]:
    """Convert the inline TeX used in the book into style runs."""

    runs: list[dict[str, Any]] = []

    def emit(text: str, style: dict[str, Any]) -> None:
        if text:
            runs.append({"t": text, **style})

    def walk(text: str, style: dict[str, Any]) -> None:
        index = 0
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                emit("".join(buffer), style)
                buffer.clear()

        while index < len(text):
            character = text[index]
            if character == "%":
                end = text.find("\n", index)
                index = len(text) if end < 0 else end + 1
                continue
            if character == "\\":
                match = re.match(r"\\([A-Za-z]+)\*?", text[index:])
                if match is None:
                    following = text[index + 1:index + 2]
                    if following == "\\":
                        flush()
                        runs.append({"br": True})
                    elif following in _ESCAPED_CHARACTERS:
                        buffer.append(_ESCAPED_CHARACTERS[following])
                    else:
                        raise TexParseError(f"无法识别的转义：\\{following}")
                    index += 2
                    continue
                name = match.group(1)
                index += match.end()
                if name in _IGNORED_COMMANDS:
                    continue
                if name in _SPACE_COMMANDS:
                    buffer.append(" ")
                    continue
                if name in _SYMBOL_COMMANDS:
                    buffer.append(_SYMBOL_COMMANDS[name])
                    if index < len(text) and text[index] == "{" and text[index:index + 2] == "{}":
                        index += 2
                    continue
                if name == "par":
                    flush()
                    runs.append({"br": True})
                    continue
                if name in {"hspace", "vspace"}:
                    _group, index = _read_group(text, _skip_spaces(text, index))
                    if name == "hspace":
                        buffer.append(" ")
                    continue
                if name == "color":
                    _group, index = _read_group(text, _skip_spaces(text, index))
                    continue
                if name == "blank":
                    _group, index = _read_group(text, _skip_spaces(text, index))
                    flush()
                    runs.append({"blank": True, **({"u": True} if style.get("u") else {})})
                    continue
                if name == "syn":
                    role, index = _read_group(text, _skip_spaces(text, index))
                    content, index = _read_group(text, _skip_spaces(text, index))
                    flush()
                    walk(content, {**style, "syn": role.strip()})
                    continue
                if name == "ensuremath":
                    content, index = _read_group(text, _skip_spaces(text, index))
                    flush()
                    walk(content, style)
                    continue
                if name in _STYLE_COMMANDS:
                    content, index = _read_group(text, _skip_spaces(text, index))
                    flush()
                    walk(content, {**style, _STYLE_COMMANDS[name]: True})
                    continue
                raise TexParseError(f"无法识别的命令：\\{name}")
            if character == "{":
                content, index = _read_group(text, index)
                flush()
                walk(content, style)
                continue
            if character == "}":
                raise TexParseError("多余的右花括号")
            if character == "$":
                raise TexParseError("题目中出现了数学模式 $")
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
            else:
                buffer.append(character)
            index += 1
        flush()

    walk(str(source or ""), {})
    return normalize_runs(runs)


def normalize_runs(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge equal-style neighbours, collapse spaces and trim breaks at the ends."""

    merged: list[dict[str, Any]] = []
    for run in runs:
        if run.get("br"):
            if merged and not merged[-1].get("br"):
                merged.append({"br": True})
            continue
        if "t" in run:
            text = re.sub(r"[ \t\r\n]+", " ", str(run["t"]))
            run = {**run, "t": text}
            if merged and "t" in merged[-1] and _style(merged[-1]) == _style(run):
                merged[-1] = {**merged[-1], "t": merged[-1]["t"] + text}
                continue
        merged.append(dict(run))
    # Spaces never start a paragraph or double up across run boundaries.
    previous_space = True
    for run in merged:
        if run.get("br"):
            previous_space = True
            continue
        if run.get("blank"):
            previous_space = False
            continue
        text = run["t"]
        if previous_space:
            text = text.lstrip(" ")
        run["t"] = text
        previous_space = text.endswith(" ") if text else previous_space
    cleaned = [run for run in merged if run.get("br") or run.get("blank") or run.get("t")]
    while cleaned and cleaned[0].get("br"):
        cleaned.pop(0)
    while cleaned and cleaned[-1].get("br"):
        cleaned.pop()
    for position in (0, -1):
        if cleaned and "t" in cleaned[position]:
            run = cleaned[position]
            run["t"] = run["t"].lstrip() if position == 0 else run["t"].rstrip()
    for index, run in enumerate(cleaned):
        if "t" in run and index + 1 < len(cleaned) and cleaned[index + 1].get("br"):
            run["t"] = run["t"].rstrip()
    return [run for run in cleaned if run.get("br") or run.get("blank") or run.get("t")]


def _style(run: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(sorted((key, value) for key, value in run.items() if key != "t"))


def runs_to_text(runs: Iterable[dict[str, Any]], *, blank: str = "_____") -> str:
    parts: list[str] = []
    for run in runs:
        if run.get("br"):
            parts.append("\n")
        elif run.get("blank"):
            parts.append(blank)
        else:
            parts.append(str(run.get("t") or ""))
    lines = [re.sub(r"[ \u00a0]+", " ", line).strip() for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line)


def _strip_layout(text: str) -> str:
    """Remove the paragraph spacing commands that frame each field."""

    value = text.strip()
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"^(?:\\par|\\smallskip|\\medskip|\\noindent|\s)+", "", value)
        value = re.sub(r"(?:\\par|\\smallskip|\\medskip|\\noindent|\s)+$", "", value)
    return value


def _split_top_level(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(text):
        if text[index] == "\\" and not text.startswith(separator, index):
            index += 2
            continue
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        elif depth == 0 and text.startswith(separator, index):
            parts.append(text[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    parts.append(text[start:])
    return parts


def _parse_options(stem_raw: str) -> tuple[str, list[dict[str, Any]], str]:
    """Remove the option table from the stem and return ``(stem, options, layout)``."""

    table = re.search(r"\\begin\{tblr\}", stem_raw)
    if table is not None:
        spec, body_start = _read_group(stem_raw, table.end())
        end = stem_raw.find("\\end{tblr}", body_start)
        if end < 0:
            raise TexParseError("选项表缺少 \\end{tblr}")
        columns = len(re.findall(r"X\[", spec)) or 1
        cells: list[str] = []
        for row in _split_top_level(stem_raw[body_start:end], "\\\\"):
            cells.extend(cell.strip() for cell in _split_top_level(row, "&"))
        options = []
        for cell in cells:
            if not cell:
                continue
            match = re.match(r"\(([A-E])\)\s*(.*)$", cell, flags=re.S)
            if match is None:
                raise TexParseError(f"选项缺少 (A)–(E) 标号：{cell[:40]}")
            options.append({"label": match.group(1), "runs": parse_inline(match.group(2))})
        stem = stem_raw[:table.start()] + stem_raw[end + len("\\end{tblr}"):]
        return stem, sorted(options, key=lambda item: item["label"]), "two_column" if columns >= 2 else "one_column"
    enum = re.search(r"\\begin\{enumerate\}", stem_raw)
    if enum is not None:
        body_start = _skip_optional(stem_raw, enum.end())
        end = stem_raw.find("\\end{enumerate}", body_start)
        if end < 0:
            raise TexParseError("选项列表缺少 \\end{enumerate}")
        items = [item.strip() for item in stem_raw[body_start:end].split("\\item")[1:]]
        options = [
            {"label": chr(ord("A") + index), "runs": parse_inline(item)}
            for index, item in enumerate(items)
        ]
        stem = stem_raw[:enum.start()] + stem_raw[end + len("\\end{enumerate}"):]
        return stem, options, "one_column"
    return stem_raw, [], ""


def _error_spans(stem_raw: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for match in LABEL_ULINE_PATTERN.finditer(stem_raw):
        content, _end = _read_group(stem_raw, match.end() - 1)
        spans.append({"label": match.group(1), "text": runs_to_text(parse_inline(content))})
    # A label the book prints without an underline (e.g. "(E) No error.") is
    # still one of the answers to choose from; keep the words that follow it.
    plain = runs_to_text(parse_inline(stem_raw))
    for match in re.finditer(r"\(([A-E])\)\s*([^()]*)", plain):
        if match.group(1) not in {span["label"] for span in spans}:
            text = re.sub(r"\s+", " ", match.group(2)).strip()
            spans.append({"label": match.group(1), "text": text[:40].rstrip(), "plain": True})
    return sorted(spans, key=lambda span: span["label"])


def _answer_label(answer: str) -> str:
    match = re.match(r"\(([A-E])\)", answer.strip())
    return match.group(1) if match else ""


def _split_topic(runs: list[dict[str, Any]]) -> tuple[str, str]:
    if any(run.get("br") for run in runs):
        index = next(i for i, run in enumerate(runs) if run.get("br"))
        return runs_to_text(runs[:index]), runs_to_text(runs[index + 1:])
    return runs_to_text(runs), ""


def _parse_block(block: str) -> dict[str, Any]:
    answer_match = ANSWER_MARKER.search(block)
    if answer_match is None:
        raise TexParseError("缺少答案行")
    stem_raw = block[:answer_match.start()]
    answer_raw, after_answer = _read_group(block, answer_match.start())
    answer_raw = re.sub(r"^\\color\{[^{}]*\}\\bfseries\s*答案\\quad", "", answer_raw)
    translation_match = TRANSLATION_MARKER.search(block, after_answer)
    if translation_match is None:
        raise TexParseError("缺少中文翻译")
    corrected_raw = block[after_answer:translation_match.start()]
    translation_raw, after_translation = _read_group(block, translation_match.end() - len("{\\color{black!75}"))
    translation_raw = re.sub(r"^\\color\{black!75\}", "", translation_raw)
    solution = SOLUTION_PATTERN.search(block, after_translation)
    if solution is None:
        raise TexParseError("缺少解析框")
    between = block[after_translation:solution.start()].strip()
    if between:
        raise TexParseError(f"翻译与解析之间有多余内容：{between[:40]}")
    leftover = re.sub(r"\\(?:par|medskip|smallskip|bigskip|clearpage|newpage|noindent)\b", "", block[solution.end():]).strip()
    if leftover:
        raise TexParseError(f"解析框之后有未识别的内容：{leftover[:40]}")
    stem_without_options, options, option_layout = _parse_options(stem_raw)
    return {
        "stem_raw": _strip_layout(stem_without_options),
        "options": options,
        "option_layout": option_layout,
        "answer": runs_to_text(parse_inline(answer_raw)),
        "corrected_runs": parse_inline(_strip_layout(corrected_raw)),
        "translation_runs": parse_inline(translation_raw),
        "explanation_runs": parse_inline(solution.group(1)),
    }


def validate_question(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for one parsed question.

    Errors mean the parser could not reproduce the question faithfully and the
    import must stop.  Warnings are anomalies of the book source itself (for
    example a question printed without a translation); the question is kept
    exactly as the source has it and the warning is shown in the preview.
    """

    errors: list[str] = []
    warnings: list[str] = []
    kind = item["question_type"]
    labels = [span["label"] for span in item.get("error_spans") or []]
    underlined = [span["label"] for span in item.get("error_spans") or [] if not span.get("plain")]
    option_labels = [option["label"] for option in item.get("options") or []]
    if kind == "error_choice":
        if item["options"]:
            errors.append("挑错题不应有选项表")
        if len(underlined) < 3 or labels != [chr(ord("A") + i) for i in range(len(labels))]:
            errors.append(f"挑错题下划线片段不足或标号不连续：{labels}")
        elif any(span.get("plain") and span["text"].casefold().rstrip(".") != "no error"
                 for span in item.get("error_spans") or []):
            warnings.append(
                f"原书源码的 A–E 标号与下划线没有一一对应（有下划线的是 {underlined}），按源码原样保留"
            )
        if item["answer_label"] not in "ABCDE" or not item["answer_label"]:
            errors.append(f"挑错题答案缺少 (A)–(E) 标号：{item['answer']}")
    elif kind == "choice":
        if not 4 <= len(option_labels) <= 5 or option_labels != [chr(ord("A") + i) for i in range(len(option_labels))]:
            errors.append(f"选择题选项应为 A–D/E，实际为 {option_labels}")
        if item["answer_label"] not in option_labels:
            errors.append(f"答案 {item['answer']} 不在选项中")
        if not any(run.get("blank") for run in item["stem_runs"]) and not item["prompt"].rstrip().endswith("?"):
            errors.append("选择题既没有挖空 \\blank{}，也不是整句判断题")
    elif kind == "correction":
        if item["options"] or labels:
            errors.append("改错题不应有选项或 A–E 片段")
    for field in ("answer", "prompt", "explanation"):
        if not str(item.get(field) or "").strip():
            errors.append(f"{field} 为空")
    if not str(item.get("translation") or "").strip():
        warnings.append("原书源码没有中文翻译")
    combined = "\n".join(
        str(item.get(field) or "")
        for field in ("prompt", "answer", "corrected", "translation", "explanation", "topic", "school")
    ) + "\n".join(runs_to_text(option["runs"]) for option in item.get("options") or [])
    for residue in ("\\", "outermarginfgcolor", "colspec", "solutionbox", "black!75"):
        if residue in combined:
            errors.append(f"残留 TeX 片段：{residue}")
    return errors, warnings


def parse_chapter_file(source_file: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse one chapter file into questions plus a list of issues."""

    chapter_match = re.match(r"(\d+)-", source_file.name)
    if chapter_match is None:
        return [], []
    chapter_number = int(chapter_match.group(1))
    text = source_file.read_text(encoding="utf-8")
    title_match = CHAPTER_PATTERN.search(text)
    chapter_title = title_match.group(1).strip() if title_match else ""
    headings = list(HEADING_PATTERN.finditer(text))
    topics: list[tuple[int, str, str]] = []
    for match in re.finditer(r"\\importantpoint\{", text):
        content, _end = _read_group(text, match.end() - 1)
        topic, note = _split_topic(parse_inline(content))
        topics.append((match.start(), topic, note))
    questions: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, heading in enumerate(headings):
        next_heading = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        boundary = BOUNDARY_PATTERN.search(text, heading.end(), next_heading)
        block_end = boundary.start() if boundary else next_heading
        number = int(heading.group(1))
        reference = f"{chapter_number:02d}-{number:03d}"
        label = heading.group(2).strip()
        school, _bar, type_label = (part.strip() for part in label.partition("|"))
        question_type = TYPE_LABELS.get(type_label, "choice")
        if type_label and type_label not in TYPE_LABELS:
            issues.append(f"{reference}：未知题型标签“{type_label}”")
        topic = next(((t, n) for position, t, n in reversed(topics) if position < heading.start()), ("", ""))
        try:
            parts = _parse_block(text[heading.end():block_end])
            stem_runs = parse_inline(parts["stem_raw"])
            spans = _error_spans(parts["stem_raw"]) if question_type == "error_choice" else []
        except TexParseError as error:
            issues.append(f"{reference}（{source_file.name}:{text.count(chr(10), 0, heading.start()) + 1}）：{error}")
            continue
        item = {
            "question_code": f"{QUESTION_CODE_PREFIX}-{reference}",
            "question_reference": reference,
            "chapter_number": chapter_number,
            "chapter_title": chapter_title,
            "question_number": number,
            "question_type": question_type,
            "type_name": TYPE_NAMES[question_type],
            "source_label": label,
            "school": school,
            "topic": topic[0],
            "topic_note": topic[1],
            "stem_runs": stem_runs,
            "options": parts["options"],
            "option_layout": parts["option_layout"],
            "error_spans": spans,
            "answer": parts["answer"],
            "answer_label": _answer_label(parts["answer"]),
            "corrected_runs": parts["corrected_runs"],
            "translation_runs": parts["translation_runs"],
            "explanation_runs": parts["explanation_runs"],
            "prompt": runs_to_text(stem_runs),
            "corrected": runs_to_text(parts["corrected_runs"]),
            "translation": runs_to_text(parts["translation_runs"]),
            "explanation": runs_to_text(parts["explanation_runs"]),
            "source_file": source_file.name,
            "source_line": text.count("\n", 0, heading.start()) + 1,
        }
        errors, warnings = validate_question(item)
        issues.extend(f"{reference}：{problem}" for problem in errors)
        item["warnings"] = warnings
        questions.append(item)
    return questions, issues


def parse_grammar_exercise_book(chapters_dir: Path | str) -> dict[str, Any]:
    """Parse every numbered chapter.

    ``issues`` are blocking parse errors and must be empty before importing;
    ``warnings`` list the source anomalies that were preserved as-is.
    """

    root = Path(chapters_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"未找到《文法解题》章节目录：{root}")
    questions: list[dict[str, Any]] = []
    issues: list[str] = []
    for source_file in sorted(root.glob("*.tex")):
        parsed, file_issues = parse_chapter_file(source_file)
        questions.extend(parsed)
        issues.extend(file_issues)
    codes = [item["question_code"] for item in questions]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        issues.append("题号重复：" + "、".join(duplicates))
    warnings = [
        f"{item['question_reference']}：{warning}"
        for item in questions for warning in item.get("warnings") or []
    ]
    return {"questions": questions, "issues": issues, "warnings": warnings}


def _squash(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def locate_pages(questions: list[dict[str, Any]], pdf_path: Path | str) -> dict[str, Any]:
    """Find the book page of each question's start in the compiled PDF.

    The search runs forward from the previous question's page, so pages stay
    monotonic.  A question whose text cannot be found inherits the page when
    its neighbours agree (``inferred``); otherwise its page stays empty.
    """

    import fitz

    with fitz.open(str(pdf_path)) as document:
        pages = [_squash(page.get_text()) for page in document]
    located: list[int | None] = []
    last = 0
    for item in questions:
        stem = _squash(item["prompt"].replace("_____", " "))
        keys = [key for key in (stem[:36], stem[8:44], _squash(item["translation"])[:16]) if len(key) >= 10]
        found = None
        # Look just after the previous question first, then the rest of the book.
        for window in (range(last, min(len(pages), last + 8)), range(last, len(pages))):
            for key in keys:
                found = next((index for index in window if key in pages[index]), None)
                if found is not None:
                    break
            if found is not None:
                break
        # The heading (number + source box) may sit at the foot of the previous page.
        school = _squash(item.get("school") or "")
        if found and school and school not in pages[found] and school in pages[found - 1] and found - 1 >= last:
            found -= 1
        located.append(None if found is None else found + 1)
        if found is not None:
            last = found
    counts = {"located": 0, "inferred": 0, "missing": 0}
    for index, item in enumerate(questions):
        page = located[index]
        status = "located"
        if page is None:
            before = next((located[i] for i in range(index - 1, -1, -1) if located[i]), None)
            after = next((located[i] for i in range(index + 1, len(located)) if located[i]), None)
            if before and after and before == after:
                page, status = before, "inferred"
            else:
                status = "missing"
        item["source_page"] = page
        item["page_status"] = status
        counts[status] += 1
    return counts


__all__ = [
    "QUESTION_CODE_PREFIX",
    "TYPE_NAMES",
    "TexParseError",
    "locate_pages",
    "normalize_runs",
    "parse_chapter_file",
    "parse_grammar_exercise_book",
    "parse_inline",
    "runs_to_text",
    "validate_question",
]
