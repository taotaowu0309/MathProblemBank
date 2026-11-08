"""Source record that ChatGPT Work hands in beside a lecture draft.

The lecture body carries no source labels, so ``SOURCES.json`` says where each
definition, result, proof and example came from.  This module checks the record
against the course's own extracted textbook pages: the cited book and page must
exist, and a short anchor copied from that page must really be on it.  Items the
writer added on its own are listed for the user, who reads the finished notes.

Only local files are read and no model is called.  The check shows that a citation
is real; it cannot show that the mathematics is right or that the prose follows the
cited page.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

SOURCES_FILENAME = "SOURCES.json"
REPORT_FILENAME = "SOURCE_REPORT.md"
READING_HEADING = "TEXTBOOK READING BEFORE DRAFTING"
TEACHING_PLAN_FIELDS = ("prerequisite", "order", "worked_example")
SCHEMA_VERSION = 1
MODES = ("follows", "expands", "lecture", "own")
KINDS = ("definition", "theorem", "proof", "example", "remark", "figure", "other")
MIN_ANCHOR_CHARACTERS = 16
MAX_ANCHOR_CHARACTERS = 300
FUZZY_MATCH_RATIO = 0.82
MAX_ERRORS_SHOWN = 25
LECTURE_FILES = ("WRITING_MATERIALS.md", "BOARD_NOTES.md", "AUDIO_TRANSCRIPT.md")
# Numbered statements in the formal theorem environments; each needs its own record.
STATEMENT_ENVIRONMENT = re.compile(
    r"\\begin\{(?:theorem|lemma|proposition|corollary|definition|example)\*?\}")
COMPANION_MODE = "textbook_exercise_companion"


def reading_applies(course: Any) -> bool:
    return str(dict(course).get("course_domain") or "math").casefold() != "english"


def normalize(text: str) -> str:
    """Compare text and formulas apart from spacing and LaTeX punctuation."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[\s{}$\\~]+", "", folded)


def anchor_on_page(anchor: str, page_text: str) -> str:
    """Return "exact" or "fuzzy" when a normalized anchor is on the page, else ""."""
    wanted, page = normalize(anchor), normalize(page_text)
    if not wanted or not page:
        return ""
    if wanted in page:
        return "exact"
    width = len(wanted)
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(wanted)
    for start in range(0, max(1, len(page) - width + 1), max(1, width // 4)):
        matcher.set_seq1(page[start:start + width])
        if matcher.ratio() >= FUZZY_MATCH_RATIO:
            return "fuzzy"
    return ""


def _usable_materials(service: Any, course_id: int) -> list[dict[str, Any]]:
    return [row for row in service.reference_materials(int(course_id))
            if str(row.get("extraction_status") or "").startswith("ready")]


def ledger_required(service: Any, subsection_id: int) -> bool:
    """A record is required for a course with extracted textbooks, except exercise companions."""
    subsection = service.subsection(int(subsection_id))
    if str(subsection["annotation_source"] or "") == COMPANION_MODE:
        return False
    return bool(_usable_materials(service, int(subsection["course_id"])))


def _catalog_books(storage: Path) -> dict[int, str]:
    try:
        catalog = json.loads((storage / "reference_materials" / "TEXTBOOK_CATALOG.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(book["material_id"]): str(book.get("book_ref") or "")
            for book in catalog.get("books", []) if book.get("material_id")}


def _primary_refs(course: Any) -> set[str]:
    try:
        context = json.loads((Path(str(course["project_dir"])) / "WORK_CONTEXT.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    return {str(item) for item in (context.get("context") or {}).get("primary_textbook_refs") or []}


def source_materials(service: Any, course_id: int) -> list[dict[str, Any]]:
    """Books a citation may name, primary first."""
    course = service.course(int(course_id))
    books = _catalog_books(Path(str(course["storage_dir"])))
    primary = _primary_refs(course)
    rows = [{
        "material_id": int(row["id"]),
        "filename": str(row["original_filename"]),
        "page_count": int(row["page_count"] or 0),
        "book_ref": books.get(int(row["id"]), ""),
        "primary": books.get(int(row["id"]), "") in primary,
        "source_sha256": str(row.get("source_sha256") or ""),
    } for row in _usable_materials(service, int(course_id))]
    return sorted(rows, key=lambda row: (not row["primary"], not row["book_ref"], row["material_id"]))


def reading_candidates(materials: list[dict[str, Any]], task_dir: Path | None) -> list[int]:
    """Include registered books and this unit's mapped notes."""
    if not any(row["book_ref"] for row in materials):
        return [row["material_id"] for row in materials]
    index = task_dir / "reference_materials" / "REFERENCE_INDEX.json" if task_dir else None
    parts = json.loads(index.read_text(encoding="utf-8")).get("parts", []) if index and index.is_file() else []
    hashes = {part["source_sha256"] for part in parts if part.get("source_sha256")}
    names = {part["source_filename"] for part in parts if part.get("source_filename")}
    return [row["material_id"] for row in materials
            if row["book_ref"] or row["source_sha256"] in hashes or row["filename"] in names]


def task_requires_reading(task_dir: Path | None) -> bool:
    task = task_dir / "TASK.md" if task_dir else None
    return bool(task and task.is_file() and READING_HEADING in task.read_text(encoding="utf-8").splitlines())


def _page_texts(storage: Path, material_id: int, *, unpaginated: bool = False) -> dict[int, str]:
    path = storage / "reference_materials" / "chunks" / f"material_{int(material_id):04d}.json"
    pages: dict[int, list[str]] = {}
    for chunk in json.loads(path.read_text(encoding="utf-8")):
        try:
            page = 1 if unpaginated else int(chunk["page_number"])
            pages.setdefault(page, []).append(str(chunk.get("content") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    return {number: "\n".join(parts) for number, parts in pages.items()}


def _lecture_text(task_dir: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in (task_dir / name for name in LECTURE_FILES) if path.is_file())


class _Checker:
    def __init__(self, service: Any, course_id: int) -> None:
        self.storage = Path(str(service.course(int(course_id))["storage_dir"]))
        self.materials = {row["material_id"]: row for row in source_materials(service, course_id)}
        self.pages: dict[int, dict[int, str]] = {}
        self.errors: list[str] = []
        self.visual: list[dict[str, Any]] = []
        self.fuzzy = 0

    def _texts(self, material_id: int) -> dict[int, str]:
        if material_id not in self.pages:
            self.pages[material_id] = _page_texts(
                self.storage, material_id, unpaginated=not self.materials[material_id]["page_count"])
        return self.pages[material_id]

    def citation(self, where: str, source: Any) -> dict[str, Any] | None:
        """Check one cited page; return the citation when it stands."""
        if not isinstance(source, dict):
            self.errors.append(f"{where}：sources 的每一项必须是对象。")
            return None
        try:
            material_id, page = int(source.get("material_id")), int(source.get("page"))
        except (TypeError, ValueError):
            self.errors.append(f"{where}：material_id 和 page 必须是整数。")
            return None
        material = self.materials.get(material_id)
        if material is None:
            self.errors.append(f"{where}：课程里没有资料 material_id={material_id}。")
            return None
        if not 1 <= page <= (material["page_count"] or 1):
            self.errors.append(f"{where}：{material['filename']} 只有 {material['page_count']} 页，没有第 {page} 页。")
            return None
        cited = {"material_id": material_id, "page": page, "filename": material["filename"],
                 "book_ref": material["book_ref"], "primary": material["primary"]}
        if not material["page_count"]:
            cited["unpaginated"] = True
        anchor = str(source.get("anchor") or "").strip()
        visual = str(source.get("visual_check") or "").strip()
        length = len(normalize(anchor))
        if anchor and not MIN_ANCHOR_CHARACTERS <= length <= MAX_ANCHOR_CHARACTERS:
            self.errors.append(f"{where}：anchor 去掉空格和 LaTeX 符号后应有 {MIN_ANCHOR_CHARACTERS}–"
                               f"{MAX_ANCHOR_CHARACTERS} 个字符，现在是 {length} 个。")
            return None
        texts = self._texts(material_id)
        match = anchor_on_page(anchor, texts.get(page, "")) if anchor else ""
        if match:
            self.fuzzy += match == "fuzzy"
            return cited | {"anchor_match": match}
        if visual and material["page_count"]:
            # Extraction can be empty or noisy, so a viewed page is accepted but shown to the user.
            self.visual.append(cited | {"where": where, "visual_check": visual})
            return cited | {"anchor_match": "visual_check"}
        elsewhere = [number for number, text in sorted(texts.items()) if anchor and normalize(anchor) in normalize(text)]
        hint = f" 这段文字实际在第 {elsewhere[0]} 页。" if elsewhere else ""
        self.errors.append(f"{where}：{material['filename']} 第 {page} 页的提取文本里找不到 anchor。{hint}"
                           "请从原页重新摘录；提取文本确有错漏时，实际看过原页后填写 visual_check。")
        return None

    def searched(self, where: str, item: dict[str, Any]) -> list[int]:
        values = item.get("searched")
        if not self.materials:
            return []
        if not isinstance(values, list) or not values:
            self.errors.append(f"{where}：必须在 searched 里列出已查过、确实没有这项内容的资料 material_id。")
            return []
        unknown = [value for value in values if not isinstance(value, int) or value not in self.materials]
        if unknown:
            self.errors.append(f"{where}：searched 里有课程不存在的资料 {unknown}。")
        return [value for value in values if value not in unknown]


def _check_reading(checker: _Checker, unit: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    label = target["label"]
    plan = unit.get("teaching_plan")
    if not isinstance(plan, dict) or any(
            not isinstance(plan.get(key), str) or not plan[key].strip() for key in TEACHING_PLAN_FIELDS):
        checker.errors.append(f"小节 {label}：teaching_plan 须填写 prerequisite、order 和 worked_example。")
    rows = unit.get("reading")
    if not isinstance(rows, list):
        checker.errors.append(f"小节 {label}：先填写 SOURCES.json 的 reading 教材阅读记录。")
        rows = []
    expected = reading_candidates(list(checker.materials.values()), target.get("task_dir"))
    seen: set[int] = set()
    results = []
    for index, row in enumerate(rows, 1):
        where = f"小节 {label} 阅读记录第 {index} 条"
        material_id = row.get("material_id") if isinstance(row, dict) else None
        if type(material_id) is not int or material_id not in checker.materials or material_id in seen:
            checker.errors.append(f"{where}：material_id 必须对应唯一的课程资料。")
            continue
        seen.add(material_id)
        status, section, note = (row.get(key) for key in ("status", "section", "note"))
        if status == "pending":
            checker.errors.append(f"{where}：资料 {material_id} 仍待阅读，完成后再检查。")
            continue
        if status not in ("use", "overlap", "defer", "out_of_scope"):
            checker.errors.append(f"{where}：status 须为 use、overlap、defer 或 out_of_scope。")
            continue
        if not all(isinstance(value, str) and value.strip() for value in (section, note)):
            checker.errors.append(f"{where}：须写明教材节或检索位置（section）及具体取舍（note）。")
            continue
        sources = row.get("sources", [])
        if not isinstance(sources, list) or (status != "out_of_scope" and not sources):
            checker.errors.append(f"{where}：读过的相关内容须有同一本资料的页码和原文锚点（sources）。")
            continue
        cited = []
        for source in sources:
            if not isinstance(source, dict) or source.get("material_id") != material_id:
                checker.errors.append(f"{where}：阅读出处的 material_id 必须与该条资料相同。")
                continue
            citation = checker.citation(where, source)
            if citation:
                cited.append(citation)
        material = checker.materials[material_id]
        results.append({"material_id": material_id, "book_ref": material["book_ref"], "filename": material["filename"],
                        "status": status, "section": section.strip(), "note": note.strip(), "sources": cited})
    missing = [str(material_id) for material_id in expected if material_id not in seen]
    if missing:
        checker.errors.append(f"小节 {label}：这些资料还没有阅读或相关性说明：{', '.join(missing)}。")
    return {"reading": results, "teaching_plan": plan}


def _check_unit(checker: _Checker, unit: dict[str, Any], task_dir: Path | None, body: str | None,
                label: str) -> dict[str, Any]:
    items = unit.get("items")
    if not isinstance(items, list) or not items:
        checker.errors.append(f"小节 {label}：items 不能为空。")
        items = []
    lecture = _lecture_text(task_dir) if task_dir else ""
    records = []
    for index, item in enumerate(items, 1):
        where = f"小节 {label} 第 {index} 条"
        if not isinstance(item, dict):
            checker.errors.append(f"{where}：必须是对象。")
            continue
        name = str(item.get("item") or "").strip()
        mode = str(item.get("mode") or "")
        kind = str(item.get("kind") or "")
        if not name:
            checker.errors.append(f"{where}：item 不能为空。")
        if mode not in MODES:
            checker.errors.append(f"{where}：mode 必须是 {'/'.join(MODES)}。")
            continue
        if kind not in KINDS:
            checker.errors.append(f"{where}：kind 必须是 {'/'.join(KINDS)}。")
        record: dict[str, Any] = {"item": name, "kind": kind, "mode": mode}
        if mode in ("follows", "expands"):
            sources = item.get("sources")
            if not isinstance(sources, list) or not sources:
                checker.errors.append(f"{where}：{mode} 必须在 sources 里写明书和页码。")
                sources = []
            record["sources"] = [cited for cited in (checker.citation(where, source) for source in sources) if cited]
            if mode == "expands":
                record["detail"] = str(item.get("detail") or "").strip()
                if not record["detail"]:
                    checker.errors.append(f"{where}：expands 必须用 detail 说明在教材基础上补了什么。")
        else:
            record["searched"] = checker.searched(where, item)
            if mode == "lecture":
                anchor = str(item.get("lecture_anchor") or "").strip()
                if len(normalize(anchor)) < MIN_ANCHOR_CHARACTERS:
                    checker.errors.append(f"{where}：lecture 必须用 lecture_anchor 摘录课堂材料原文，"
                                          f"至少 {MIN_ANCHOR_CHARACTERS} 个字符。")
                elif not anchor_on_page(anchor, lecture):
                    checker.errors.append(f"{where}：lecture_anchor 不在本节课堂材料里。")
            else:
                record["reason"] = str(item.get("reason") or "").strip()
                if not record["reason"]:
                    checker.errors.append(f"{where}：own 必须用 reason 说明为什么要自己补写。")
        records.append(record)
    if body is not None:
        if re.search(r"\\begin\{(?:exercise|solution)\*?\}", body):
            checker.errors.append(f"小节 {label}：网课讲义不写习题和解答，习题由用户另外安排。")
        statements = len(STATEMENT_ENVIRONMENT.findall(body))
        if len(records) < statements:
            checker.errors.append(f"小节 {label}：正文有 {statements} 个定义、定理、例子或习题环境，"
                                  f"来源清单只有 {len(records)} 条；每个都要有记录。")
    return {"label": label, "items": records}


def check_record(service: Any, course_id: int, record: Any,
                 units: list[dict[str, Any]], *, reading_only: bool = False) -> dict[str, Any]:
    """Check a parsed record against ``units`` ({subsection_id, label, task_dir, body})."""
    if not isinstance(record, dict) or not isinstance(record.get("units"), list):
        raise ValueError(f"{SOURCES_FILENAME} 顶层必须是含 units 数组的对象。")
    given = [entry.get("subsection_id") if isinstance(entry, dict) else None for entry in record["units"]]
    expected = [int(unit["subsection_id"]) for unit in units]
    if given != expected:
        raise ValueError(f"{SOURCES_FILENAME} 的 units 必须恰好按顺序对应这些小节：应为 {expected}，实际为 {given}。")
    checker = _Checker(service, course_id)
    reports = []
    for entry, unit in zip(record["units"], units):
        reading = _check_reading(checker, entry, unit) if (
            reading_only or "reading" in entry or unit.get("require_reading")
            or task_requires_reading(unit.get("task_dir"))) else {}
        if reading_only:
            reports.append({"label": unit["label"], **reading})
            continue
        report = _check_unit(checker, entry, unit.get("task_dir"), unit.get("body"), str(unit["label"]))
        if reading:
            cited_ids = {source["material_id"] for item in report["items"] for source in item.get("sources", [])}
            used_ids = {row["material_id"] for row in reading["reading"] if row["status"] == "use"}
            for material_id in sorted(used_ids - cited_ids):
                checker.errors.append(
                    f"小节 {unit['label']}：资料 {material_id} 标记为 use，但正文来源条目没有引用它。")
            for material_id in sorted(cited_ids - used_ids):
                checker.errors.append(f"小节 {unit['label']}：正文引用了资料 {material_id}，请将阅读决定记为 use。")
        reports.append({**report, **reading})
    if checker.errors:
        shown = checker.errors[:MAX_ERRORS_SHOWN]
        more = len(checker.errors) - len(shown)
        raise ValueError(f"{SOURCES_FILENAME} 未通过（{len(checker.errors)} 处）：\n" + "\n".join(shown)
                         + (f"\n……另有 {more} 处" if more else ""))
    if reading_only:
        return {"reading_checked": True, "units": reports, "visual_checks": checker.visual, "model_calls": 0}
    warnings = []
    has_primary = any(material["primary"] for material in checker.materials.values())
    for report in reports:
        cited = [source for item in report["items"] for source in item.get("sources", [])]
        if has_primary and not any(source["primary"] for source in cited):
            warnings.append(f"小节 {report['label']} 没有引用主教材。")
    items = [item for report in reports for item in report["items"]]
    counts = {mode: sum(item["mode"] == mode for item in items) for mode in MODES}
    return {"units": reports, "counts": counts, "total_items": len(items), "fuzzy_anchor_matches": checker.fuzzy,
            "visual_checks": checker.visual, "warnings": warnings, "model_calls": 0}


def load_record(path: Path) -> Any:
    if not path.is_file():
        raise ValueError(f"缺少来源清单 {path}；写法见任务说明或指南3.0b。")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        raise ValueError(f"{path.name} 不是合法 JSON：{error}") from error


def check_subsection_reading(service: Any, subsection_id: int) -> dict[str, Any]:
    subsection = service.subsection(subsection_id)
    course_id = int(subsection["course_id"])
    if not reading_applies(service.course(course_id)) or not ledger_required(service, subsection_id):
        return {"reading_checked": False, "reason": "此任务没有需要此步骤的数学教材资料。", "model_calls": 0}
    task_dir = service.subsection_task_dir(subsection_id)
    if not (task_dir / "TASK.md").is_file():
        raise ValueError("小节任务还没生成；先运行 course task。")
    return check_record(service, course_id, load_record(task_dir / SOURCES_FILENAME), [
        {"subsection_id": subsection_id, "task_dir": task_dir, "label": str(subsection["stable_key"])}
    ], reading_only=True)


def _source_label(source: dict[str, Any]) -> str:
    book = source.get("book_ref") or source.get("filename") or ""
    if source.get("unpaginated"):
        return f"{book}（无页码）"
    return f"{book} 第 {source['page']} 页"


def render_report(report: dict[str, Any], title: str) -> str:
    """The list the user reads beside the finished notes; own additions come first."""
    counts = report["counts"]
    lines = [f"# {title} 来源清单", "",
             f"共 {report['total_items']} 条：沿用教材 {counts['follows']}，在教材基础上展开 {counts['expands']}，"
             f"来自课堂 {counts['lecture']}，自己补写 {counts['own']}。", "",
             "程序只核对书、页码和摘录确实存在，不证明数学正确，也不证明正文忠于所引的页。", ""]
    lines += ["## 自己补写（建议重点阅读）", ""]
    own = [(unit["label"], item) for unit in report["units"] for item in unit["items"] if item["mode"] == "own"]
    lines += [f"- {label}：{item['item']}。原因：{item.get('reason', '')}" for label, item in own] or ["无。"]
    lines += ["", "## 未能用文本核对、由写作者声明看过原页", ""]
    lines += [f"- {entry['where']}：{_source_label(entry)}。{entry['visual_check']}"
              for entry in report["visual_checks"]] or ["无。"]
    if report["warnings"]:
        lines += ["", "## 提醒", "", *[f"- {warning}" for warning in report["warnings"]]]
    lines += ["", "## 逐节明细", ""]
    for unit in report["units"]:
        lines += [f"### {unit['label']}", ""]
        if "reading" in unit:
            plan = unit["teaching_plan"]
            lines += [f"先修回顾：{plan['prerequisite']}", f"讲解顺序：{plan['order']}",
                      f"算例或关键中间计算：{plan['worked_example']}", ""]
            for row in unit["reading"]:
                book = row["book_ref"] or row["filename"]
                pages = "、".join(str(source["page"]) for source in row["sources"] if not source.get("unpaginated"))
                lines.append(f"- 阅读 [{row['status']}] {book}，{row['section']}"
                             + (f"（锚点物理页 {pages}）" if pages else "") + f"。{row['note']}")
            lines.append("")
        for item in unit["items"]:
            sources = "；".join(_source_label(source) for source in item.get("sources", []))
            detail = f"（补充：{item['detail']}）" if item.get("detail") else ""
            lines.append(f"- [{item['mode']}] {item['item']}" + (f" — {sources}" if sources else "") + detail)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def writing_policy_text() -> str:
    """Source order; it overrides older copies of the contracts inside a task folder."""
    return "\n".join([
        "SOURCE ORDER (user policy; overrides any older wording in the copied contracts)",
        "",
        "The recording (and its teacher-only draft, when there is one) decides the scope, the topic order and the teacher's own",
        "explanation, which stays in the text. For each",
        "definition, statement, proof and example inside that scope, work in this order:",
        "1. Write it the way the primary textbook does, after reading the page itself.",
        "2. Compare the relevant reference books even when the primary treatment is complete.",
        "   Integrate clearer explanations, prerequisites, worked examples, alternate proofs or figures.",
        "   Locate relevant unmapped sections with TEXTBOOK_GUIDE.md, the index or search tools.",
        "   Compare hypotheses and notation before merging. Scope still comes from the recording.",
        "3. Add detail on top of a source where it skips a step, and record that as `expands`.",
        "4. Write something yourself only after steps 1 and 2 found nothing, and record it as `own`.",
        "Never write from memory what a page in the course's sources already says. If the teacher's",
        "version differs from the books, say so in the text rather than silently choosing one.",
    ])


def reading_policy_text() -> str:
    return f"""{READING_HEADING}

ChatGPT Work prepares the reading record in SOURCES.json for a math lecture with imported references
before writing OUTPUT.tex. Use the recording topics and the full course book list to
locate and read the relevant passages, including unmapped ones. Search hits and titles
locate pages. Read the relevant exposition in full and inspect original pages where
formulas, diagrams or extraction are uncertain. Compare the mathematical content.

For each unit, fill `reading` with one entry per candidate (all imported registered
books and this unit's mapped sources, or all ready materials if book IDs are absent).
Include other relevant sources you find. Each entry has `material_id`, `section` (section
and physical pages actually read, or the index/search consulted), `status` and a concrete
`note` explaining what the source contributes or why you will omit it. Use `use`,
`overlap` or `defer` with same-material `sources` citations for relevant passages read.
Use `out_of_scope` only after checking relevance and explain the search and result.
Record unavailable or unread relevant material as `pending` and explain the obstacle.
Complete the reading before checking again.

In `teaching_plan`, give `prerequisite` (what to recall), `order` (the exposition order
and likely sticking points), and `worked_example` (a concrete example or intermediate
calculation).
Prefer a familiar example before an abstract proof where it helps. Plan full
derivations and explain what each construction accomplishes. Keep exercises separate.

Run the registered check_online_course_reference_reading operation before drafting.
The `items` list may be empty at this stage. While writing, record the actual contributions
of each source marked `use` in `items`. Update decisions if they change. The local check
verifies the record and page anchors. The writer must actually read and compare the pages.

Before the import check, ChatGPT Work compares each finished subsection with the passages
already read in the primary textbook, reference books, and author notes. Look for missing
explanations, proof steps, worked examples, counterexamples, in-scope results, or helpful
figures. Integrate useful additions into OUTPUT.tex and update the affected items and
reading notes in SOURCES.json, including reasons to defer nearby material. The writer
does this self-check as part of writing.

For every adopted passage in the primary and reference books, check the original
page and its continuation pages for associated diagrams and include those originals.
Similar diagrams in other books do not replace this coverage. Explain notation,
record each source page, original asset hash and body placement, and state concrete
reasons for excluding unadopted neighboring content. Check native-scale bounds,
readability and page breaks in the actual formal PDF after import.

Compare the full scope of each relevant source result with the body, including
general versions of special cases, equivalent formulations, nearby consequences
and explicit constructions. For each candidate, identify the named result and its
actual use or a concrete reason and destination for deferring it in reading.note.
Overlapping definitions do not make every result in a source section redundant.

Use the reader's recorded mathematical background to choose prerequisite reminders.
For each substantive proof step, explain its justification and check the hypotheses.
Point to a specific earlier result when available. State the full needed version of
any nontrivial result used without a prior proof and check its conditions on the
current objects. Prove the local lemmas and intermediate computations needed here.
For substantial external prerequisites, identify the assumption and give a verified
source and proof location. Check choices, well-definedness, map properties,
quantifiers, both implication directions, and circular dependencies.
"""


def record_format_text(materials: list[dict[str, Any]], subsection_ids: list[int], *,
                       reading: bool = False) -> str:
    """How to write SOURCES.json, with this course's book identifiers."""
    table = [f"- material_id {row['material_id']}: {row['filename']}, {row['page_count']} pages"
             + (f", {row['book_ref']}" if row["book_ref"] else "") + (" (PRIMARY)" if row["primary"] else "")
             for row in materials]
    skeleton = json.dumps({"schema_version": SCHEMA_VERSION, "units": [
        {"subsection_id": subsection_id, **({"reading": [], "teaching_plan": dict.fromkeys(TEACHING_PLAN_FIELDS, "")}
                                          if reading else {}),
         "items": []} for subsection_id in subsection_ids]}, ensure_ascii=False)
    return "\n".join([
        f"SOURCE RECORD (`{SOURCES_FILENAME}`, required beside `OUTPUT.tex`)",
        "",
        "The lecture body keeps no source labels; this file holds them. Write it while you read, not",
        "afterwards. Importing is refused while it is missing or does not check out.",
        "",
        f"Skeleton: `{skeleton}`",
        "",
        "One item for each definition, theorem, lemma, proposition, corollary and example,",
        "and for each proof or figure with a source of its own. Every item has `item` (what it is, e.g.",
        f"\"Proposition: Hom and products\"), `kind` ({'/'.join(KINDS)}) and `mode`:",
        "- `follows`: taken from a book. Needs `sources`.",
        "- `expands`: a book's treatment with added steps. Needs `sources` and `detail` (what was added).",
        "- `lecture`: from the teacher, and not in the books. Needs `lecture_anchor` (words copied from",
        "  this subsection's WRITING_MATERIALS.md, BOARD_NOTES.md or AUDIO_TRANSCRIPT.md) and `searched`.",
        "- `own`: written by you. Needs `reason` and `searched`. The user is shown every `own` item.",
        "",
        "`sources` is a list of {`material_id`, `page`, `anchor`}. `page` is the physical PDF page.",
        "For an unpaginated text file, use `page: 1` to look up a passage in the whole document.",
        f"`anchor` is {MIN_ANCHOR_CHARACTERS}–{MAX_ANCHOR_CHARACTERS} characters copied from that page's "
        "extracted text in",
        "`parts/`; the application looks for it on that exact page. If the extraction is empty or wrong",
        "there, view the page and give `visual_check` (what you saw) instead; these are shown to the user.",
        "`searched` lists the material_id values you actually looked in without finding the item.",
        "",
        "Books of this course:",
        *table,
    ])
