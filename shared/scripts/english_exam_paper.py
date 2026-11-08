"""Exam papers (考研英语一/二 and similar) for the English question bank.

An exam paper arrives as one structured JSON object, usually written by the
AI assistant after reading the paper's PDF (see :data:`EXAM_PAPER_FORMAT`).
This module validates that object and turns it into rows for
``english_question_groups`` (passages shared by several questions) and
``english_question_bank_items`` (one row per question).  The database writes
themselves stay in :class:`EnglishLearningService`, so the Qt button and the
registered AI operation share one executor.

Text fields use a small markup that an AI can write reliably and that maps to
the same style runs as the 《文法解题》 importer:

* a blank line starts a new paragraph, a single line break is kept;
* ``[[12]]`` is the numbered blank 12 of a cloze passage, ``____`` an unnumbered blank;
* ``<u>…</u>`` underlines (e.g. a sentence to translate), ``**…**`` highlights.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

QUESTION_TYPES = {
    "cloze": ("reading", "完形填空", True),
    "reading_choice": ("reading", "阅读选择", True),
    "paragraph_order": ("reading", "段落排序", True),
    "matching": ("reading", "信息匹配", True),
    "translation": ("writing", "翻译", False),
    "writing": ("writing", "写作", False),
}
EXAM_SHORT_NAMES = {"考研英语一": "英语一", "考研英语二": "英语二"}
PAPER_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{4}")
GROUP_KEY_PATTERN = re.compile(r"[A-Za-z0-9_]+")
OPTION_LABELS = "ABCDEFGH"

EXAM_PAPER_EXAMPLE: dict[str, Any] = {
    "exam": "考研英语一",
    "year": 2025,
    "paper_code": "KY-E1-2025",
    "title": "2025 年全国硕士研究生招生考试英语（一）",
    "source_pdf": "<原卷 PDF 的本机绝对路径>",
    "answer_source": "非官方参考答案（两家公开答案交叉核对）",
    "groups": [
        {
            "key": "S1",
            "section": "Section I 完形填空",
            "title": "Section I Use of English",
            "directions": "Read the following text. Choose the best word(s) for each numbered blank…",
            "passage": "Located in the southern Peloponnesian peninsula, … This area is [[1]] to earthquakes…",
            "source_page": 1,
        },
        {
            "key": "PB",
            "section": "Part B 段落排序",
            "title": "Section II Part B",
            "directions": "The following paragraphs are given in a wrong order…",
            "options": [{"label": "A", "text": "Peters likes to photograph butterflies…"}],
            "order": "41 → 42 → C → 43 → H → 44 → A → 45",
        },
    ],
    "questions": [
        {
            "number": 1, "group": "S1", "type": "cloze",
            "options": [{"label": "A", "text": "relevant"}, {"label": "B", "text": "prone"},
                        {"label": "C", "text": "available"}, {"label": "D", "text": "alien"}],
            "answer": "B",
            "explanation": "be prone to 意为“易于遭受”，后接 earthquakes and tsunamis。",
            "explanation_source": "ai",
            "source_page": 2,
        },
        {
            "number": 46, "group": "PC", "type": "translation",
            "stem": "Recent decades have seen science move into a convention where …",
            "reference": "近几十年来，……",
            "explanation": "see … move into 结构：时间作主语……",
            "explanation_source": "ai",
        },
        {
            "number": 52, "type": "writing", "section": "Part B 大作文",
            "stem": "Write an essay of 160-200 words based on the following table…",
            "table": {"caption": "近年来全国居民平均每百户年末主要耐用消费品拥有量",
                      "header": ["年份", "空调（台）"], "rows": [["2014", "75.2"]]},
            "reference": "As is shown in the table, …",
            "explanation_source": "ai",
        },
    ],
}

EXAM_PAPER_FORMAT: dict[str, Any] = {
    "tool_name": "import_english_exam_paper",
    "format": "structured_json_exam_paper",
    "workflow": [
        "1. 用户提供试卷 PDF 的绝对路径后，用 read_exam_pdf_text 读出全文（需要时分页读）。",
        "2. 按本格式整理整套试卷：每篇文章一个 group，每道题一个 question，题号与原卷一致。",
        "3. 客观题答案必须来自用户提供的答案或可靠的公开答案，并在 answer_source 写明来源；"
        "不能确认的答案留空，不要猜。主观题（翻译、写作）的 reference 是参考译文/范文。",
        "4. 先调用 preview_english_exam_paper 把预览结果给用户看，用户确认后再调用 import_english_exam_paper。",
    ],
    "paper_fields": {
        "exam": "考试名称，如“考研英语一”“考研英语二”（必填，卡片上显示为“2025 英语一”）",
        "year": "年份，四位数字（必填）",
        "paper_code": "试卷编号，大写字母/数字加年份，如 KY-E1-2025、KY-E2-2024（必填，题号为 paper_code-两位题号）",
        "title": "试卷全称（必填）",
        "source_pdf": "原卷 PDF 的路径（可选，卡片上的“原卷”按钮会打开它）；写文件名时按试卷 JSON 所在目录解析",
        "answer_source": "答案来源说明，例如“非官方参考答案（……交叉核对）”（有客观题答案时必填）",
    },
    "group_fields": {
        "key": "本卷内唯一的组标识，如 S1、T1、T2、PB、PC（字母数字）",
        "section": "部分名称，作为筛选里的“考点/部分”，如“Section I 完形填空”“Part A Text 1”",
        "title": "显示的标题", "directions": "题目要求（可选）",
        "passage": "文章正文（用标记语法；完形的空写成 [[题号]]）",
        "options": "段落排序等题的候选段落 [{label: A–H, text}]（可选）",
        "order": "段落排序的版面提示，如 “41 → 42 → C → 43 …”（可选）",
        "source_page": "文章在原卷 PDF 中的页码（可选）",
    },
    "question_fields": {
        "number": "原卷题号（必填，整卷唯一）",
        "type": "cloze | reading_choice | paragraph_order | matching | translation | writing",
        "group": "所属文章组的 key（完形、阅读、排序、翻译题必填）",
        "section": "没有 group 的题（如写作）填部分名称",
        "stem": "题干（完形题可省略；翻译题写要翻译的原句；写作题写题目要求和材料）",
        "options": "客观题选项 [{label, text}]，完形/阅读为 A–D，排序/匹配可到 H",
        "answer": "客观题答案字母；不能确认时留空",
        "reference": "主观题参考译文或范文",
        "table": "写作题的表格 {caption, header: [...], rows: [[...]]}（可选）",
        "explanation": "解析（可选，简短中文）",
        "explanation_source": "解析来源：ai（AI 撰写）| official（官方）| user（用户提供）",
        "source_page": "题目所在原卷页码（可选）",
    },
    "markup": {
        "段落": "空一行开始新段落；单个换行会保留（用于书信、列表），所以 PDF 里的自动折行要先接回成整句",
        "[[12]]": "第 12 题的编号空格（完形）",
        "____": "不带编号的空格",
        "<u>…</u>": "下划线（如翻译题的划线句）",
        "**…**": "加深强调",
    },
    "categories": "完形填空、阅读选择、段落排序、信息匹配归入“阅读”类；翻译、写作归入“写作”类。",
    "example": EXAM_PAPER_EXAMPLE,
}


def markup_to_runs(text: Any) -> list[dict[str, Any]]:
    """Turn the exam markup into the style runs used by the question cards."""

    value = str(text or "").replace("\r\n", "\n").strip()
    runs: list[dict[str, Any]] = []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value) if part.strip()]
    token = re.compile(r"\[\[(\d+)\]\]|_{3,}|<u>(.*?)</u>|\*\*(.*?)\*\*", re.S)
    for index, paragraph in enumerate(paragraphs):
        if index:
            runs.append({"br": True})
        paragraph = re.sub(r"[ \t]*\n[ \t]*", "\n", paragraph)
        position = 0
        for match in token.finditer(paragraph):
            if match.start() > position:
                runs.append({"t": paragraph[position:match.start()]})
            if match.group(1):
                runs.append({"blank": True, "n": int(match.group(1))})
            elif match.group(0).startswith("_"):
                runs.append({"blank": True})
            elif match.group(2) is not None:
                runs.append({"t": match.group(2), "u": True})
            else:
                runs.append({"t": match.group(3), "em": True})
            position = match.end()
        if position < len(paragraph):
            runs.append({"t": paragraph[position:]})
    # A single line break inside a paragraph (a letter, a numbered list) stays a line break.
    split: list[dict[str, Any]] = []
    for run in runs:
        if "t" in run and "\n" in run["t"]:
            for index, piece in enumerate(run["t"].split("\n")):
                if index:
                    split.append({"line": True})
                split.append({**run, "t": piece})
        else:
            split.append(run)
    return [run for run in split if run.get("br") or run.get("line") or run.get("blank") or run.get("t")]


def runs_plain(runs: Any) -> str:
    parts: list[str] = []
    for run in runs or []:
        if run.get("br") or run.get("line"):
            parts.append("\n")
        elif run.get("blank"):
            parts.append(f"__{run['n']}__" if run.get("n") else "_____")
        else:
            parts.append(str(run.get("t") or ""))
    return "\n".join(re.sub(r"[ \u00a0]+", " ", line).strip() for line in "".join(parts).split("\n")).strip()


def load_paper(paper: Any = None, paper_file: str | Path | None = None) -> dict[str, Any]:
    base: Path | None = None
    if paper_file:
        path = Path(str(paper_file)).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"试卷 JSON 文件不存在：{path}")
        paper = json.loads(path.read_text(encoding="utf-8"))
        base = path.resolve().parent
    if isinstance(paper, str):
        paper = json.loads(paper)
    if not isinstance(paper, Mapping):
        raise ValueError("试卷必须是一个 JSON 对象（见 get_english_exam_import_format）。")
    paper = dict(paper)
    # A relative source_pdf sits beside the JSON file, so the pair can be copied between machines.
    source_pdf = str(paper.get("source_pdf") or "").strip()
    if source_pdf and base is not None and not Path(source_pdf).is_absolute() and not re.match(r"[A-Za-z]:[\\/]", source_pdf):
        paper["source_pdf"] = str(base / source_pdf)
    return paper


def _options(raw: Any, where: str, issues: list[str]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for option in raw or []:
        label = str((option or {}).get("label") or "").strip().upper()
        if len(label) != 1 or label not in OPTION_LABELS:
            issues.append(f"{where}：选项标号必须是 A–H，实际为“{label}”")
            continue
        options.append({"label": label, "runs": markup_to_runs(option.get("text"))})
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        issues.append(f"{where}：选项标号重复 {labels}")
    return options


def build_exam_rows(paper: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one paper and build group and question rows.

    ``issues`` block the import; ``warnings`` (for example an objective
    question without an answer) are shown in the preview.
    """

    issues: list[str] = []
    warnings: list[str] = []
    exam = str(paper.get("exam") or "").strip()
    title = str(paper.get("title") or "").strip()
    paper_code = str(paper.get("paper_code") or "").strip().upper()
    try:
        year = int(paper.get("year"))
    except (TypeError, ValueError):
        year = 0
    if not exam:
        issues.append("缺少 exam（考试名称，如“考研英语一”）")
    if not 1990 <= year <= 2100:
        issues.append("year 必须是四位年份")
    if not PAPER_CODE_PATTERN.fullmatch(paper_code) or (year and not paper_code.endswith(str(year))):
        issues.append("paper_code 应形如 KY-E1-2025（大写字母/数字，以年份结尾）")
    if not title:
        issues.append("缺少 title（试卷全称）")
    short = EXAM_SHORT_NAMES.get(exam, exam.replace("考研", ""))
    paper_label = f"{year} {short}".strip()
    source_pdf = str(paper.get("source_pdf") or "").strip()
    answer_source = str(paper.get("answer_source") or "").strip()

    groups: dict[str, dict[str, Any]] = {}
    for raw in paper.get("groups") or []:
        key = str((raw or {}).get("key") or "").strip()
        if not GROUP_KEY_PATTERN.fullmatch(key) or key in groups:
            issues.append(f"文章组 key 无效或重复：“{key}”")
            continue
        where = f"文章组 {key}"
        content = {
            "version": 1,
            "exam": exam, "year": year, "paper_code": paper_code, "paper_label": paper_label,
            "section": str(raw.get("section") or "").strip(),
            "title": str(raw.get("title") or "").strip(),
            "directions": markup_to_runs(raw.get("directions")),
            "passage": markup_to_runs(raw.get("passage")),
            "options": _options(raw.get("options"), where, issues),
            "order": str(raw.get("order") or "").strip(),
        }
        if not content["passage"] and not content["options"]:
            issues.append(f"{where}：没有正文 passage，也没有候选段落 options")
        groups[key] = {
            "group_code": f"{paper_code}-{key}",
            "category": "",
            "title": content["title"] or content["section"],
            "passage": runs_plain(content["passage"]),
            "content": content,
            "source_title": title,
            "source_path": source_pdf,
            "source_page": raw.get("source_page"),
        }

    questions: list[dict[str, Any]] = []
    numbers: set[int] = set()
    for raw in paper.get("questions") or []:
        try:
            number = int((raw or {}).get("number"))
        except (TypeError, ValueError):
            issues.append(f"题目缺少整数题号：{str(raw)[:60]}")
            continue
        where = f"第 {number} 题"
        if number in numbers:
            issues.append(f"{where}：题号重复")
            continue
        numbers.add(number)
        kind = str(raw.get("type") or "").strip()
        if kind not in QUESTION_TYPES:
            issues.append(f"{where}：type 必须是 {'、'.join(QUESTION_TYPES)} 之一")
            continue
        category, type_name, objective = QUESTION_TYPES[kind]
        group_key = str(raw.get("group") or "").strip()
        group = groups.get(group_key) if group_key else None
        if group_key and group is None:
            issues.append(f"{where}：group “{group_key}” 不存在")
            continue
        if kind in {"cloze", "reading_choice", "paragraph_order", "translation"} and group is None:
            issues.append(f"{where}：{type_name}题必须属于一个文章组")
            continue
        if group is not None:
            if group["category"] and group["category"] != category:
                issues.append(f"{where}：同一文章组里混有阅读类和写作类题目")
            group["category"] = category
        section = str(raw.get("section") or "").strip() or (group["content"]["section"] if group else "")
        stem = markup_to_runs(raw.get("stem"))
        if kind == "cloze" and not stem:
            stem = [{"t": f"第 {number} 空"}]
        options = _options(raw.get("options"), where, issues)
        if kind == "paragraph_order" and not options and group is not None:
            options = list(group["content"]["options"])
        answer = str(raw.get("answer") or "").strip()
        answer_label = ""
        if objective:
            if len(options) < 2:
                issues.append(f"{where}：客观题至少要有两个选项")
            labels = [option["label"] for option in options]
            match = re.fullmatch(r"\(?([A-H])\)?", answer.upper())
            if answer and (match is None or match.group(1) not in labels):
                issues.append(f"{where}：答案“{answer}”不在选项 {labels} 中")
            elif match is not None:
                answer_label = match.group(1)
            else:
                warnings.append(f"{where}：没有答案，做题时只能对照解析自己判断")
        elif not stem:
            issues.append(f"{where}：{type_name}题必须有题干 stem")
        reference = markup_to_runs(raw.get("reference"))
        explanation = markup_to_runs(raw.get("explanation"))
        explanation_source = str(raw.get("explanation_source") or ("ai" if explanation or reference else "")).strip()
        table = raw.get("table") if isinstance(raw.get("table"), Mapping) else None
        option_text = {option["label"]: runs_plain(option["runs"]) for option in options}
        answer_text = answer_label and f"{answer_label}" or answer
        if answer_label and answer_label in option_text and kind != "paragraph_order":
            answer_text = f"{answer_label}. {option_text[answer_label]}"
        content = {
            "version": 1,
            "exam": exam, "year": year, "paper_code": paper_code, "paper_title": title,
            "paper_label": paper_label, "school": paper_label,
            "question_number": number, "type_name": type_name, "section": section,
            "group_key": group_key,
            "stem": stem,
            "options": options,
            "option_layout": "two_column" if options and all(len(text) <= 30 for text in option_text.values()) else "one_column",
            "answer": answer_text, "answer_label": answer_label,
            "answer_note": answer_source if objective and answer_label else "",
            "model_answer": reference,
            "reference_label": "参考译文" if kind == "translation" else ("参考范文" if kind == "writing" else ""),
            "explanation": explanation,
            "explanation_source": explanation_source,
            "table": dict(table) if table else None,
            "warnings": [],
        }
        prompt_lines = [runs_plain(stem)] + [f"{label}. {text}" for label, text in option_text.items()]
        row = {
            "category": category,
            "question_code": f"{paper_code}-{number:02d}",
            "title": f"{paper_label} 第 {number} 题",
            "prompt": "\n".join(line for line in prompt_lines if line),
            "passage": "",
            "answer_text": answer_text or runs_plain(reference)[:2000],
            "explanation_text": runs_plain(explanation),
            "source_title": title,
            "source_path": source_pdf,
            "source_page": raw.get("source_page") or (group or {}).get("source_page"),
            "source_line": None,
            "metadata_json": json.dumps(
                {"importer": "exam_paper_v1", "paper_code": paper_code, "answer_source": answer_source},
                ensure_ascii=False, sort_keys=True,
            ),
            "question_type": kind,
            "topic": section,
            "chapter_number": None,
            "source_label": paper_label,
            "group_key": group_key,
            # Newer papers first, questions in paper order.
            "sort_key": 100_000 + (3000 - year) * 1000 + number,
            "content_json": json.dumps(content, ensure_ascii=False, sort_keys=True),
            "exam": exam,
            "exam_year": year,
        }
        questions.append(row)
    if not questions:
        issues.append("试卷中没有题目 questions")
    has_objective_answer = any(json.loads(row["content_json"])["answer_label"] for row in questions)
    if has_objective_answer and not answer_source:
        issues.append("有客观题答案时必须在 answer_source 写明答案来源")
    for group in groups.values():
        if not group["category"]:
            warnings.append(f"文章组 {group['group_code']} 没有任何题目引用，不会导入")
        content = group.pop("content")
        group["content_json"] = json.dumps(content, ensure_ascii=False, sort_keys=True)
    used_groups = [group for group in groups.values() if group["category"]]
    for row in questions + used_groups:
        # The PDF path differs per machine (and per copy of a package); it is not content.
        content_only = {key: value for key, value in row.items() if key != "source_path"}
        digest_source = json.dumps(content_only, ensure_ascii=False, sort_keys=True, default=str)
        row["content_sha256"] = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    by_type: dict[str, int] = {}
    for row in questions:
        by_type[row["question_type"]] = by_type.get(row["question_type"], 0) + 1
    return {
        "exam": exam, "year": year, "paper_code": paper_code, "title": title, "paper_label": paper_label,
        "groups": used_groups, "questions": questions, "by_type": by_type,
        "issues": issues, "warnings": warnings,
    }


def read_pdf_text(pdf_path: str | Path, *, first_page: int = 1, last_page: int | None = None,
                  max_chars: int = 60_000) -> dict[str, Any]:
    """Plain text of a range of PDF pages, for the AI to structure an exam paper."""

    import fitz

    path = Path(str(pdf_path)).expanduser()
    if path.suffix.casefold() != ".pdf" or not path.is_file():
        raise FileNotFoundError(f"PDF 不存在：{path}")
    pages: list[dict[str, Any]] = []
    total = 0
    with fitz.open(str(path)) as document:
        count = int(document.page_count)
        start = max(1, int(first_page))
        end = min(count, int(last_page or count))
        for number in range(start, end + 1):
            text = re.sub(r"[ \t]+", " ", document[number - 1].get_text())
            text = re.sub(r" *\n *", "\n", text).strip()
            if total + len(text) > max_chars and pages:
                break
            pages.append({"page": number, "text": text})
            total += len(text)
    last_read = pages[-1]["page"] if pages else start - 1
    return {
        "pdf_path": str(path), "page_count": count, "pages": pages,
        "next_page": last_read + 1 if last_read < count else None,
        "text_layer": any(page["text"] for page in pages),
    }


EXAM_IMPORT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "templates" / "exam_paper_import_prompt.txt"


def exam_import_prompt(pdf_path: str = "") -> str:
    """The AI assistant's step-by-step guide for importing one exam paper."""

    text = EXAM_IMPORT_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return text.replace("{{PDF}}", str(pdf_path or "").strip() or "（在这里给出试卷 PDF 的完整路径）") + "\n"


def _words(text: str) -> list[str]:
    normalized = str(text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return [word.casefold() for word in re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", normalized)]


def check_exam_paper(paper: Mapping[str, Any], pdf_path: str | Path | None = None) -> dict[str, Any]:
    """Check an exam paper before it is imported.

    Besides the schema check of ``build_exam_rows`` this compares every
    passage, stem and option word by word with the PDF's text layer (words
    missing from the PDF point at typos or rewritten text), and checks that
    cloze blanks match the questions, numbering has no gaps and the answers
    look plausible.  ``problems`` must be empty before importing.
    """

    import difflib

    built = build_exam_rows(paper)
    problems: list[str] = list(built["issues"])
    notes: list[str] = list(built["warnings"])
    questions = list(paper.get("questions") or [])
    groups = {str(group.get("key") or ""): group for group in paper.get("groups") or []}

    numbers = sorted(int(item.get("number")) for item in questions if str(item.get("number", "")).isdigit())
    if numbers:
        gaps = [number for number in range(numbers[0], numbers[-1] + 1) if number not in numbers]
        if gaps:
            notes.append(f"题号不连续，缺少：{gaps}（若原卷本来如此可忽略）")
    for key, group in groups.items():
        blanks = sorted(int(number) for number in re.findall(r"\[\[(\d+)\]\]", str(group.get("passage") or "")))
        if blanks:
            cloze = sorted(int(item["number"]) for item in questions
                           if item.get("group") == key and item.get("type") == "cloze")
            if blanks != cloze:
                problems.append(f"文章组 {key} 的空格编号 {blanks} 与完形题题号 {cloze} 不一致")
    for item in questions:
        if item.get("type") == "translation" and item.get("group") in groups:
            sentence = re.sub(r"</?u>", "", str(item.get("stem") or "")).strip()
            passage = re.sub(r"</?u>", "", str(groups[item["group"]].get("passage") or ""))
            if sentence and " ".join(_words(sentence)) not in " ".join(_words(passage)):
                problems.append(f"第 {item.get('number')} 题：翻译句在文章组 {item['group']} 的原文里找不到")
    answers = [str(item.get("answer") or "").strip().upper().strip("()") for item in questions
               if item.get("type") in {"cloze", "reading_choice"} and item.get("answer")]
    if len(answers) >= 10:
        most = max(set(answers), key=answers.count)
        if answers.count(most) / len(answers) > 0.5:
            notes.append(f"客观题答案有一半以上是 {most}，请再核对答案来源")

    mismatches: list[dict[str, Any]] = []
    if pdf_path:
        pages = read_pdf_text(pdf_path, max_chars=10_000_000)["pages"]
        pdf_words = _words("\n".join(page["text"] for page in pages))

        def compare(label: str, text: str) -> None:
            cleaned = re.sub(r"\[\[\d+\]\]|_{3,}|</?u>|\*\*|\(\d{2}\)", " ", str(text or ""))
            words = [word for word in _words(cleaned) if not re.fullmatch(r"\d{1,2}", word)]
            if not words:
                return
            matcher = difflib.SequenceMatcher(None, pdf_words, words, autojunk=False)
            missing: list[str] = []
            cursor = 0
            matched = 0
            for block in matcher.get_matching_blocks():
                if block.b > cursor:
                    missing.append(" ".join(words[cursor:block.b]))
                matched += block.size
                cursor = block.b + block.size
            if missing:
                mismatches.append({"item": label, "matched": matched, "words": len(words), "not_in_pdf": missing[:8]})

        for key, group in groups.items():
            compare(f"文章组 {key} 正文", str(group.get("passage") or ""))
            for option in group.get("options") or []:
                compare(f"文章组 {key} 段落 {option.get('label')}", str(option.get("text") or ""))
        for item in questions:
            if item.get("type") in {"translation", "writing"}:
                continue
            text = " ".join([str(item.get("stem") or "")] + [str(option.get("text") or "") for option in item.get("options") or []])
            if item.get("type") != "paragraph_order":
                compare(f"第 {item.get('number')} 题题干与选项", text)
        for entry in mismatches:
            problems.append(
                f"{entry['item']}：有 {entry['words'] - entry['matched']} 个词在 PDF 里对不上，如 "
                + "；".join(entry["not_in_pdf"][:3])
            )
    return {
        "paper_code": built["paper_code"], "question_count": len(built["questions"]),
        "problems": problems, "notes": notes, "text_mismatches": mismatches,
        "ok": not problems,
    }


__all__ = [
    "EXAM_PAPER_FORMAT",
    "QUESTION_TYPES",
    "build_exam_rows",
    "check_exam_paper",
    "exam_import_prompt",
    "load_paper",
    "markup_to_runs",
    "read_pdf_text",
    "runs_plain",
]
