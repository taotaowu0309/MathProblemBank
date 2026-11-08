"""Command line for ChatGPT Work: prepare writing tasks and collect what it wrote.

ChatGPT Work runs this from the project folder (see docs/CHATGPT_WORK_GUIDE.zh-CN.md).
Every command prints one JSON object.  Commands that write formal data call the
same service methods as the control center, so validation, backups, readback
and PDF compilation are unchanged; ``check`` commands only validate.

    .venv/bin/python -m shared.scripts.chatgpt_work <area> <command> [...]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Callable

from shared.scripts import chatgpt_work_activity

# Progress lines of the current command, kept for the activity log.
_PROGRESS: list[str] = []


def _emit(message: str) -> None:
    _PROGRESS.append(f"{datetime.now():%H:%M:%S} {message}")
    print(message, file=sys.stderr, flush=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "keys") and hasattr(value, "__getitem__"):
        return {str(key): _jsonable(value[key]) for key in value.keys()}
    return value


def _drop(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if key not in keys}


# -- services ------------------------------------------------------------------

def _online_courses() -> Any:
    from shared.scripts.online_course_service import OnlineCourseService

    return OnlineCourseService()


def _translations() -> Any:
    from shared.scripts.translation_project_service import TranslationProjectService

    return TranslationProjectService()


def _notes() -> Any:
    from shared.scripts.textbook_notes_service import TextbookNotesService

    return TextbookNotesService()


def _english() -> Any:
    from shared.scripts.english_learning_service import EnglishLearningService

    return EnglishLearningService()


def _problem_bank() -> Any:
    from shared.scripts.problem_bank_center_qt import SUBJECTS, DashboardService

    return DashboardService(SUBJECTS)


# -- online courses and exercise companions -----------------------------------

def course_list(_args: argparse.Namespace) -> Any:
    service = _online_courses()
    with service.connect(rows=True) as connection:
        rows = connection.execute(
            "SELECT id,course_code,title,course_mode,status FROM online_courses ORDER BY id"
        ).fetchall()
    return {"courses": [dict(row) for row in rows]}


def course_units(args: argparse.Namespace) -> Any:
    service = _online_courses()
    units = []
    for row in service.course_subsections(int(args.course_id)):
        subsection_id = int(row["id"])
        task = service.chatgpt_work_task(subsection_id)
        units.append({
            "subsection_id": subsection_id,
            "stable_key": row["stable_key"],
            "chapter": row["chapter_title"],
            "section": row["section_title"],
            "subsection": row["subsection_title"],
            "annotation_source": row["annotation_source"],
            "has_recordings": bool(service.subsection_segments(subsection_id)),
            "task_ready": task["task_ready"],
            "draft_exists": task["draft_exists"],
        })
    status = {
        row.get("subsection_id"): row.get("latex_import_status")
        for row in service.episode_subsection_display_rows(int(args.course_id))
        if row.get("subsection_id")
    }
    for unit in units:
        unit["latex_import_status"] = status.get(unit["subsection_id"], "")
    return {"course_id": int(args.course_id), "units": units}


def course_task(args: argparse.Namespace) -> Any:
    """The current task folder of a subsection, as the user prepared it in the control center."""
    service = _online_courses()
    subsection_id = int(args.subsection_id)
    subsection = service.subsection(subsection_id)
    if str(subsection["annotation_source"] or "") == "textbook_exercise_companion":
        # A companion folder is assembled locally (no model call) and must carry the current PDF.
        service.prepare_subsection_chatgpt_package(subsection_id, _emit)
    task = _drop(service.chatgpt_work_task(subsection_id, include_reference_reading=True), "draft_source")
    if not task["task_ready"]:
        task["hint"] = ("任务文件夹还没生成。先用 workflow start --course-id 和 get_online_course_processing_status 查录制材料；"
                        "锁定证据已就绪时运行 course prepare。")
    return task


def course_prepare(args: argparse.Namespace) -> Any:
    service = _online_courses()
    result = service.rebuild_subsection_chatgpt_package(
        int(args.subsection_id), _emit, local_only=True
    )
    return {**_drop(service.chatgpt_work_task(int(args.subsection_id), include_reference_reading=True),
                    "draft_source"), "build": result}


def course_check(args: argparse.Namespace) -> Any:
    service = _online_courses()
    subsection_id = int(args.subsection_id)
    task = service.chatgpt_work_task(subsection_id)
    if not task["draft_exists"]:
        raise ValueError(f"还没有 OUTPUT.tex：{task['output_file']}")
    sources = service.check_chatgpt_work_sources(subsection_id, str(task["draft_source"]))
    recordings = service.subsection_recording_rows(subsection_id)
    if not recordings:
        return {"checked": False, "sources": sources, "reason": "参考资料或习题集小节没有单独的预检；collect 时会校验，失败不会写入。"}
    result = service.validate_recording_segments_latex(
        subsection_id, [str(item["session_id"]) for item in recordings], str(task["draft_source"])
    )
    from shared.scripts.online_course_episode_task import figure_summary
    figures = figure_summary(service, int(service.subsection(subsection_id)["course_id"]), [str(task["draft_source"])])
    return {"checked": True, "sources": sources, "figures": figures, **_drop(result, "source")}


def course_collect(args: argparse.Namespace) -> Any:
    return _online_courses().import_chatgpt_work_draft(int(args.subsection_id), _emit, compile_full_course=True)


def course_intake_task(args: argparse.Namespace) -> Any:
    from shared.scripts.online_course_work_intake import prepare_intake_task
    return prepare_intake_task(_online_courses(), int(args.course_id), int(args.episode), _emit)


def course_intake_apply(args: argparse.Namespace) -> Any:
    from shared.scripts.online_course_work_intake import apply_intake
    return apply_intake(_online_courses(), int(args.course_id), int(args.episode), _emit)


def course_episode_task(args: argparse.Namespace) -> Any:
    from shared.scripts.online_course_episode_task import prepare_episode_task
    return prepare_episode_task(_online_courses(), int(args.course_id), int(args.episode))


def course_episode_check(args: argparse.Namespace) -> Any:
    from shared.scripts.online_course_episode_task import check_episode_output
    return check_episode_output(_online_courses(), int(args.course_id), int(args.episode))


def course_episode_collect(args: argparse.Namespace) -> Any:
    from shared.scripts.online_course_episode_task import collect_episode_output
    return collect_episode_output(_online_courses(), int(args.course_id), int(args.episode), _emit)


def course_textbook_excerpt(args: argparse.Namespace) -> Any:
    request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
    return _online_courses().prepare_textbook_excerpt(**request)


def textbook_evidence(args: argparse.Namespace) -> Any:
    """Expose existing registered textbook read/derived tools, never registration writes."""
    from shared.scripts.ai_agent_repository import AiAgentToolExecutor, GlobalProblemRepository
    request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
    name = request["tool"]
    if name not in {"list_textbooks", "get_textbook_dataset_status", "search_textbook_content",
                    "get_textbook_index_health", "verify_textbook_index_hit",
                    "render_textbook_pages_for_ai", "list_unrecognized_textbook_pages"}:
        raise ValueError("Only existing textbook evidence/health tools are available here.")
    executor = AiAgentToolExecutor(GlobalProblemRepository())
    executor.begin_turn("Read registered textbook evidence for the requested course", {}, write_authorized=False)
    return executor.execute(name, request.get("arguments", {}))


# Work orchestrates the registered business capabilities; it does not receive a
# raw SQL/shell/delete escape hatch through this command.
_WORKFLOW_TOOLS = {
    "get_workflow_context", "create_study_subject", "create_study_project",
    "register_study_textbook", "download_study_reference", "save_workflow_context",
    "list_subjects", "list_projects", "list_textbooks", "get_textbook_dataset_status",
    "get_textbook_index_health", "complete_textbook_ocr", "repair_failed_textbook_pages",
    "rebuild_textbook_index", "list_unrecognized_textbook_pages", "verify_textbook_index_hit",
    "rebind_textbook_pdf", "search_textbook_content", "render_textbook_pages_for_ai",
    "list_online_courses", "create_online_course", "get_online_course_processing_status",
    "list_online_course_reference_materials", "import_online_course_reference_materials",
    "reanalyze_online_course_reference_materials", "prepare_online_course_recording",
    "get_online_course_media_engine_status", "get_online_course_lecture_outline",
    "configure_online_course_lecture_outline",
    "prepare_online_course_episode_package", "prepare_online_course_subsection_package",
    "prepare_online_course_packages", "prepare_online_course_textbook_excerpt",
    "prepare_online_course_episode_intake", "apply_online_course_episode_intake",
    "prepare_online_course_episode_writing_task", "check_online_course_episode_writing_output",
    "check_online_course_reference_reading",
    "import_online_course_episode_writing_output",
    "prepare_online_course_textbook_catalog",
    "save_online_course_page_recoveries",
    "get_online_course_chatgpt_work_task", "list_ai_reference_materials", "read_ai_reference_material",
    # Reading the textbooks, references, existing problems and lectures Work writes from.
    "get_textbook_pdf_outline", "read_local_pdf_pages", "read_local_pdf_evidence_batch",
    "list_project_files", "read_project_file", "get_library_overview", "search_problems", "get_problem",
    "get_problem_evidence_batch", "resolve_problem_reference", "get_project_problems",
    "get_online_course_subsection_latex", "search_online_course_lecture_pdf",
    "read_online_course_lecture_pdf_pages", "get_online_course_diagram_backend_status",
    "get_online_course_retention_status",
    "get_online_course_source_evidence",
    # The writer's own checks of formulas and figures (not an independent model audit).
    "symbolic_math", "numerical_math", "verify_formula", "find_counterexample",
    "render_math_figure_preview", "validate_math_figure", "compile_online_course_subsection_latex_preview",
    # Formal outputs through the existing builders.
    "crop_problem_textbook_figure", "prepare_problem_import_chatgpt_work_task",
    "build_project_pdf", "compile_online_course_pdf",
    # What used to be automatic model calls; Work writes these now.
    "get_vocabulary_etymology_evidence", "save_vocabulary_etymology",
    "read_audiobook_chapter", "save_audiobook_chapter_translation",
    "prepare_translation_second_layer_prompt", "save_translation_second_layer_result",
    "get_translation_context_task", "save_translation_context", "save_translation_paratext",
    "preview_reading_help_marks", "import_reading_help_marks",
}
def _workflow_executor() -> Any:
    from shared.scripts.ai_agent_repository import AiAgentToolExecutor, GlobalProblemRepository
    return AiAgentToolExecutor(GlobalProblemRepository())


def workflow_start(args: argparse.Namespace) -> Any:
    executor = _workflow_executor()
    executor.begin_turn("Read current project workflow state", {}, write_authorized=False)
    return executor.execute("get_workflow_context", {
        **({"subject_name": args.subject} if args.subject else {}),
        **({"project_ref": args.project} if args.project else {}),
        **({"course_id": args.course_id} if args.course_id else {}),
    })


def workflow_catalog(_args: argparse.Namespace) -> Any:
    from shared.scripts.ai_agent_repository import AI_OPERATION_REGISTRY
    return {"operations": [row for row in AI_OPERATION_REGISTRY.catalog() if row["name"] in _WORKFLOW_TOOLS]}


def workflow_describe(args: argparse.Namespace) -> Any:
    if args.tool_name not in _WORKFLOW_TOOLS:
        raise ValueError("This operation is not exposed by the Work workflow entry.")
    executor = _workflow_executor()
    executor.begin_turn("Read operation schema", {}, write_authorized=False)
    return executor.execute("describe_registered_operation", {"tool_name": args.tool_name})


def workflow_run(args: argparse.Namespace) -> Any:
    request = json.loads(Path(args.request_file).read_text(encoding="utf-8-sig"))
    name, arguments = request["tool"], request.get("arguments", {})
    if name not in _WORKFLOW_TOOLS:
        raise ValueError("This operation is not exposed by the Work workflow entry.")
    executor = _workflow_executor()
    executor.begin_turn(str(request.get("user_request") or "Execute the selected Work workflow operation"), {},
                        write_authorized=bool(args.authorize_write))
    executor.set_progress_callback(_emit)
    if args.authorize_write:
        def authorized_preview(preview: dict[str, Any]) -> bool:
            _emit("已授权操作预览：" + json.dumps(preview, ensure_ascii=False, default=str))
            return True
        executor.set_mutation_approval_callback(authorized_preview)
    result = executor.execute(name, arguments)
    if not result.get("ok"):
        raise ValueError(result.get("error") or "Workflow operation failed.")
    return result["data"]


def companion_directory_prepare(args: argparse.Namespace) -> Any:
    task = _online_courses().prepare_companion_directory_task(int(args.course_id), int(args.chapter))
    return _drop(task, "draft_source")


def companion_directory_collect(args: argparse.Namespace) -> Any:
    return _online_courses().import_companion_directory_draft(int(args.course_id), int(args.chapter))


# -- translations ----------------------------------------------------------------

def translation_list(_args: argparse.Namespace) -> Any:
    return {"projects": [
        {key: item.get(key) for key in ("code", "title")}
        for item in _translations().list_projects()
    ]}


def translation_units(args: argparse.Namespace) -> Any:
    units = _translations().list_units(args.project, args.granularity)
    return {"project": args.project, "units": [
        {key: unit.get(key) for key in (
            "target_id", "target_kind", "number", "title", "agent_status",
            "package_status", "latex_status", "compile_status",
        )}
        for unit in units
    ]}


def translation_prepare(args: argparse.Namespace) -> Any:
    service = _translations()
    service.generate_material_package(args.project, args.target, _emit)
    return _drop(service.chatgpt_work_task(args.project, args.target), "draft_source")


def translation_collect(args: argparse.Namespace) -> Any:
    return _translations().import_chatgpt_work_draft(args.project, args.target, _emit)


# -- problem bank direct import --------------------------------------------------

def _problem_sources_text(subject: str, project: str | None, books: list[str] | None) -> str:
    """SOURCES.md of a standard-problem task: which textbooks, where, and how to read them."""
    from shared.scripts.chatgpt_workflow import storage_map
    executor = _workflow_executor()
    executor.begin_turn("Prepare standard problem sources", {}, write_authorized=False)
    textbooks = executor.repository.list_textbooks(subject)["textbooks"]
    wanted = [code for code in (books or []) if code]
    unknown = [code for code in wanted if code not in {b["book_code"] for b in textbooks}]
    if unknown:
        raise ValueError("这些教材编号未在本学科登记：" + "、".join(unknown) + "。先 workflow start --subject 查看已登记教材。")
    project_dir = None
    handoff: dict[str, Any] = {}
    project_code = ""
    if project:
        project_dir, row = executor.repository._project_directory(subject, project)
        project_code = str(row["collection_code"])
        context_file = project_dir / "WORK_CONTEXT.json"
        if context_file.is_file():
            handoff = json.loads(context_file.read_text(encoding="utf-8")).get("context", {})
        if not wanted:
            wanted = [str(code) for code in handoff.get("primary_textbook_refs", [])]
    storage = storage_map(executor, subject, project_dir, project_code)
    selected = [b for b in textbooks if b["book_code"] in wanted]
    others = [b for b in textbooks if b["book_code"] not in wanted]

    def book_line(book: dict[str, Any]) -> str:
        state = "PDF可用" if book.get("pdf_available") else "PDF缺失（先用 rebind_textbook_pdf 找回）"
        return (f"- `{book['book_code']}` {book.get('title') or ''} — {book.get('author') or ''}"
                f" {book.get('edition') or ''}；{state}\n  PDF：`{book.get('pdf_path') or ''}`")

    scratch = storage["work_scratch"]["path"]
    lines = [f"# 写作依据：{subject}", ""]
    if selected:
        lines += ["## 本次依据的教材", "", *[book_line(b) for b in selected], ""]
    else:
        lines += ["## 本次依据的教材", "", "用户没有指定教材编号。按对话中的要求从下面已登记教材中选；"
                  "要用新书时先下载/登记（见 docs/CHATGPT_WORK_GUIDE.zh-CN.md 第0节）。", ""]
    if others:
        lines += ["## 本学科其他已登记教材（可作参考教材）", "", *[book_line(b) for b in others], ""]
    if project_dir is not None:
        references = sorted(p.name for p in (project_dir / "references").glob("*") if p.is_file())
        lines += [f"## 学习项目 {project_code}", "", f"- 项目目录：`{storage['project']['folder']}`",
                  f"- 参考资料目录：`{storage['project']['references_dir']}`（{len(references)} 个文件）",
                  *[f"  - `{name}`" for name in references[:60]],
                  f"- 交接记录：`{storage['project']['handoff_file']}`" + ("（已存在，先读）" if handoff else "（尚无）"), ""]
    lines += [
        "## 怎样读教材",
        "",
        f"请求 JSON 放在 `{scratch}/`，用 `workflow run <请求.json>` 执行（参数见 `workflow describe <工具名>`）：",
        "",
        "1. `get_textbook_index_health`：确认所选教材的索引/OCR状态；缺页按指南补 OCR，不重建全部教材。",
        "2. `get_textbook_pdf_outline`（书签）、`search_textbook_content`、`verify_textbook_index_hit`：定位定义、定理、例题和习题的精确物理页。",
        "3. `render_textbook_pages_for_ai`：每次最多 4 页高清页图，实际查看后才算读过原页和原图；"
        "未登记的参考 PDF 用 `read_local_pdf_pages` 读文字。",
        "4. `search_problems`、`get_project_problems`：查已有标准题，避免重复导入同一道题。",
        "5. 公式和计算可用 `symbolic_math`、`numerical_math`、`verify_formula`、`find_counterexample` 自行核对（写作者自查，不是独立审计）。",
        "",
        "## 教材原图",
        "",
        f"原图裁切到 `{storage['subject']['problem_figures_dir']}/`，题干/解答中写 "
        "`\\includegraphics[width=0.6\\linewidth]{problems/<文件名>.pdf}`，并在图注或 [Notes] 写明教材编号和页码。请求示例：",
        "",
        "```json",
        json.dumps({"tool": "crop_problem_textbook_figure", "arguments": {
            "subject_name": subject, "book_ref": (selected[0]["book_code"] if selected else "<教材编号>"),
            "source_pdf": (selected[0].get("pdf_path") if selected else "<已登记PDF路径>"),
            "page": 12, "bbox": [72, 300, 520, 560], "filename": "<教材编号>_p12_fig1.pdf",
            "title": "Figure 1.2"}}, ensure_ascii=False, indent=2),
        "```",
        "",
        "bbox 是 PDF 点坐标（左、上、右、下）：页图像素坐标 × 72 ÷ dpi（dpi 见 render 结果和文件名）。裁切后渲染或打开裁切 PDF 核对；需 `--authorize-write`。"
        "教材这一页没有图就不插图，不自行重画冒充原图。",
        "",
    ]
    return "\n".join(lines)


def problems_prepare(args: argparse.Namespace) -> Any:
    sources = _problem_sources_text(args.subject, args.project, args.book)
    task = _problem_bank().chatgpt_work_import_task(args.subject, prepare=True, sources_text=sources)
    return _drop(task, "draft_source")


def problems_check(args: argparse.Namespace) -> Any:
    service = _problem_bank()
    task = service.chatgpt_work_import_task(args.subject)
    if not task["draft_exists"]:
        raise ValueError(f"还没有 IMPORT.txt：{task['output_file']}")
    values = service.parse_direct_canonical_templates(str(task["draft_source"]))
    return {"problem_count": len(values), "titles": [str(item.get("title") or "") for item in values]}


def problems_collect(args: argparse.Namespace) -> Any:
    service = _problem_bank()
    task = service.chatgpt_work_import_task(args.subject)
    if not task["draft_exists"]:
        raise ValueError(f"还没有 IMPORT.txt：{task['output_file']}")
    text = str(task["draft_source"])
    if args.collection_id and args.build_pdf:
        result = service.import_direct_templates_and_build_pdf(args.subject, int(args.collection_id), text, _emit)
    else:
        backup, codes, created_ids = service.import_direct_canonical_templates(args.subject, text)
        result = {"backup": backup, "problem_codes": codes, "created_ids": created_ids}
        if args.collection_id:
            result["added_to_collection"] = service.add_canonical_ids_to_collection(
                args.subject, int(args.collection_id), created_ids
            )
    return {**result, "archived_draft": service.archive_chatgpt_work_import(args.subject, text)}


# -- English notes, exam papers and writing grading -------------------------------

def notes_outline(args: argparse.Namespace) -> Any:
    return {"pdf": args.pdf, "bookmarks": _notes().pdf_outline(args.pdf)}


def notes_textbook_prepare(args: argparse.Namespace) -> Any:
    service = _notes()
    outline = {int(item["index"]): item for item in service.pdf_outline(args.pdf)}
    chosen = [outline[int(index)] for index in args.bookmark or [] if int(index) in outline]
    task = service.prepare_textbook_note_task(
        args.pdf,
        book=args.book or Path(args.pdf).stem,
        bookmarks=[item["path_title"] for item in chosen],
        page_start=min((item["page_start"] for item in chosen), default=int(args.first_page or 0)),
        page_end=max((item["page_end"] for item in chosen), default=int(args.last_page or 0)),
        ideas=args.ideas or "",
    )
    return _drop(task, "draft_source")


def _english_question(category: str, code: str) -> tuple[Any, dict[str, Any]]:
    english = _english()
    question = english.question_bank_item(question_code=code, category=category)
    if question is None:
        raise ValueError(f"英语题库中没有题号 {code}（分类 {category}）。")
    return english, question


def notes_question_prepare(args: argparse.Namespace) -> Any:
    english, question = _english_question(args.category, args.code)
    link = english.question_note_link(question)
    task = _notes().prepare_question_note_task(
        link["question_key"],
        book=str(question.get("source_title") or ""),
        question=english.question_prompt_text(question),
        ideas=args.ideas or "",
    )
    return {**_drop(task, "draft_source"), "question_link": link}


def _create_note_from_draft(kind: str, key: str, title: str, **links: Any) -> Any:
    service = _notes()
    task = service.note_task(kind, key)
    if not task["draft_exists"]:
        raise ValueError(f"还没有 OUTPUT.tex：{task['output_file']}")
    note = service.create_note(title, str(task["draft_source"]), source_kind="ai", compile_pdf=False, **links)
    compiled = service.compile_note(int(note["id"]), emit=_emit)
    archived = service.archive_note_draft(kind, key)
    return {"note": note, "compile": compiled, "archived_draft": archived}


def notes_textbook_collect(args: argparse.Namespace) -> Any:
    service = _notes()
    key = service.textbook_note_task(args.pdf)["task_key"]
    links = [
        {"pdf_path": str(Path(args.pdf).resolve()), "outline_index": int(index), "book_title": args.book or Path(args.pdf).stem}
        for index in args.bookmark or []
    ]
    if not links:
        raise ValueError("至少用 --bookmark 指定一个要链接的书签序号（见 notes outline）。")
    return _create_note_from_draft("textbook", key, args.title, links=links)


def notes_question_collect(args: argparse.Namespace) -> Any:
    english, question = _english_question(args.category, args.code)
    link = english.question_note_link(question)
    return _create_note_from_draft("question", link["question_key"], args.title, question_links=[link])


def exam_prepare(args: argparse.Namespace) -> Any:
    return _english().prepare_exam_import_task(args.pdf)


def exam_tasks(_args: argparse.Namespace) -> Any:
    service = _english()
    return {"tasks": [service.exam_import_task(folder) for folder in service.exam_import_task_dirs()]}


def exam_check(args: argparse.Namespace) -> Any:
    return _drop(_english().check_exam_import_draft(args.task_dir), "paper")


def exam_import(args: argparse.Namespace) -> Any:
    to_inbox = sys.platform == "darwin" if args.to_inbox is None else args.to_inbox == "yes"
    return _english().import_exam_import_draft(args.task_dir, to_inbox=to_inbox)


def _reading_help() -> Any:
    from shared.scripts.reading_help_service import ReadingHelpService

    return ReadingHelpService()


def reading_list(args: argparse.Namespace) -> Any:
    return {"documents": _reading_help().list_documents(pending_only=not args.all)}


def reading_marks(args: argparse.Namespace) -> Any:
    service = _reading_help()
    row = service._resolve_document(args.document)
    return {"document": row, "marks": [
        _drop(mark, "boxes", "text_key") for mark in service.marks_for_document(int(row["id"]))
    ]}


def reading_task(args: argparse.Namespace) -> Any:
    return _reading_help().prepare_task(args.document, mark_ids=args.mark)


def reading_check(args: argparse.Namespace) -> Any:
    result = _reading_help().check_task(args.task)
    return _drop(result, "items")


def reading_collect(args: argparse.Namespace) -> Any:
    return _reading_help().collect_task(args.task)


def grading_prepare(args: argparse.Namespace) -> Any:
    english, question = _english_question("writing", args.code)
    response = args.response or (
        Path(args.response_file).read_text(encoding="utf-8") if args.response_file else ""
    )
    return english.prepare_writing_grading_task(int(question["id"]), response)


# -- parser ------------------------------------------------------------------------

def activity_show(args: argparse.Namespace) -> Any:
    timeline = chatgpt_work_activity.read_timeline(args.date, args.machine)
    if args.last:
        timeline["timeline"] = timeline["timeline"][-args.last:]
    return timeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chatgpt_work", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    areas = parser.add_subparsers(dest="area", required=True)

    def command(area: Any, name: str, handler: Callable[[argparse.Namespace], Any], help_text: str) -> argparse.ArgumentParser:
        sub = area.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    course = areas.add_parser("course", help="网课讲义（含参考资料小节和教材习题集小节）").add_subparsers(dest="command", required=True)
    command(course, "list", course_list, "列出全部网课和教材习题集")
    command(course, "units", course_units, "列出一门课的小节与写作状态").add_argument("course_id", type=int)
    command(course, "task", course_task, "读取小节当前的任务文件夹（习题集小节会当场生成）").add_argument("subsection_id", type=int)
    sub = command(course, "prepare", course_prepare, "用已就绪的锁定证据本地重建小节材料")
    sub.add_argument("subsection_id", type=int)
    sub.add_argument("--local-only", action="store_true", help="只用已就绪的锁定证据本地重建；不调用模型，不支持参考资料重映射")
    command(course, "check", course_check, "只校验 OUTPUT.tex，不写入").add_argument("subsection_id", type=int)
    command(course, "collect", course_collect, "正式导入 OUTPUT.tex 并重新编译整门课 PDF").add_argument("subsection_id", type=int)
    for name, handler, help_text in (
        ("intake-task", course_intake_task, "把刚录好的一集交给 Work：转写、候选帧缩略图、教材目录（不调用模型）"),
        ("intake-apply", course_intake_apply, "校验 Work 的 INTAKE.json 并生成这一集的材料和小节任务（不调用模型）"),
        ("episode-task", course_episode_task, "把一整集的小节合成一个写作任务（规则和往期讲义只读一次，不调用模型）"),
        ("episode-check", course_episode_check, "按标记拆分整集 OUTPUT.tex 并逐节本地检查，不写入"),
        ("episode-collect", course_episode_collect, "逐节检查后导入整集，最后只编译一次整门课 PDF"),
    ):
        sub = command(course, name, handler, help_text)
        sub.add_argument("course_id", type=int)
        sub.add_argument("episode", type=int, help="分集序号（第几集）")
    command(course, "textbook-excerpt", course_textbook_excerpt,
            "本地附加精确教材页和原图（每次最多4页，不调用模型）").add_argument("request_file")
    command(course, "textbook-evidence", textbook_evidence,
            "通过现有注册工具读取已登记教材、索引和原页").add_argument("request_file")

    workflow = areas.add_parser("workflow", help="从教材和项目创建到写作交付的Work入口").add_subparsers(dest="command", required=True)
    sub = command(workflow, "start", workflow_start, "新对话先读入口、交接记录及正式项目进度；零模型调用")
    sub.add_argument("--subject")
    sub.add_argument("--project")
    sub.add_argument("--course-id", type=int)
    command(workflow, "catalog", workflow_catalog, "列出Work可调用的现有业务能力和权限")
    command(workflow, "describe", workflow_describe, "读取某项业务能力的实际参数结构").add_argument("tool_name")
    sub = command(workflow, "run", workflow_run, "执行一项已注册业务操作；请求由Work准备")
    sub.add_argument("request_file")
    sub.add_argument("--authorize-write", action="store_true", help="当前用户已授权该正式操作时由Work使用")

    companion = areas.add_parser("companion", help="教材习题集讲义的章目录").add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("directory-prepare", companion_directory_prepare, "生成一章目录任务"),
        ("directory-collect", companion_directory_collect, "正式导入 DIRECTORY.txt"),
    ):
        sub = command(companion, name, handler, help_text)
        sub.add_argument("course_id", type=int)
        sub.add_argument("chapter", type=int)

    translation = areas.add_parser("translation", help="教材翻译").add_subparsers(dest="command", required=True)
    command(translation, "list", translation_list, "列出译本项目")
    sub = command(translation, "units", translation_units, "列出一个译本的节（或 --granularity subsection 列小节）")
    sub.add_argument("project")
    sub.add_argument("--granularity", choices=["section", "subsection"], default="section")
    for name, handler, help_text in (
        ("prepare", translation_prepare, "生成目标的任务文件夹（第二层产物过期时报错，由 Work 校正后再生成）"),
        ("collect", translation_collect, "正式导入 OUTPUT.tex 并从头编译译本 PDF"),
    ):
        sub = command(translation, name, handler, help_text)
        sub.add_argument("project")
        sub.add_argument("target")
        
    problems = areas.add_parser("problems", help="题库标准题直接导入").add_subparsers(dest="command", required=True)
    sub = command(problems, "prepare", problems_prepare, "生成导入任务（模板、规范、章节清单、教材依据 SOURCES.md）")
    sub.add_argument("subject")
    sub.add_argument("--project", help="学习项目编号；读取其参考资料和交接记录里的主教材")
    sub.add_argument("--book", action="append", help="本次依据的已登记教材编号，可重复")
    command(problems, "check", problems_check, "只解析 IMPORT.txt，报告可导入题数").add_argument("subject")
    collect = command(problems, "collect", problems_collect, "正式导入 IMPORT.txt")
    collect.add_argument("subject")
    collect.add_argument("--collection-id", type=int)
    collect.add_argument("--build-pdf", action="store_true")

    notes = areas.add_parser("notes", help="英语教材笔记与题目笔记").add_subparsers(dest="command", required=True)
    command(notes, "outline", notes_outline, "列出教材 PDF 的书签序号与页码").add_argument("pdf")
    sub = command(notes, "textbook-prepare", notes_textbook_prepare, "生成教材笔记任务（截取原书页）")
    sub.add_argument("pdf")
    sub.add_argument("--book")
    sub.add_argument("--bookmark", type=int, action="append")
    sub.add_argument("--first-page", type=int)
    sub.add_argument("--last-page", type=int)
    sub.add_argument("--ideas")
    sub = command(notes, "textbook-collect", notes_textbook_collect, "把 OUTPUT.tex 存成笔记、链接书签并编译")
    sub.add_argument("pdf")
    sub.add_argument("--title", required=True)
    sub.add_argument("--book")
    sub.add_argument("--bookmark", type=int, action="append")
    for name, handler, help_text in (
        ("question-prepare", notes_question_prepare, "生成题目笔记任务"),
        ("question-collect", notes_question_collect, "把 OUTPUT.tex 存成题目笔记并编译"),
    ):
        sub = command(notes, name, handler, help_text)
        sub.add_argument("category", choices=["grammar", "reading", "writing"])
        sub.add_argument("code")
        if name == "question-prepare":
            sub.add_argument("--ideas")
        else:
            sub.add_argument("--title", required=True)

    exam = areas.add_parser("exam", help="英语试卷导入").add_subparsers(dest="command", required=True)
    command(exam, "prepare", exam_prepare, "为试卷 PDF 生成任务").add_argument("pdf")
    command(exam, "tasks", exam_tasks, "列出已有试卷任务")
    command(exam, "check", exam_check, "核对 EXAM.json（有问题写 CHECK_REPORT.md）").add_argument("task_dir")
    sub = command(exam, "import", exam_import, "导入题库；Mac 上默认放进导入收件箱")
    sub.add_argument("task_dir")
    sub.add_argument("--to-inbox", choices=["yes", "no"])

    activity = areas.add_parser("activity", help="Work 调用本命令行的活动记录（自动写入，只读查看）").add_subparsers(dest="command", required=True)
    sub = command(activity, "show", activity_show, "按时间列出某天的调用、耗时和两次调用之间的间隔")
    sub.add_argument("--date", help="YYYY-MM-DD，默认今天")
    sub.add_argument("--machine", help="机器名，默认全部")
    sub.add_argument("--last", type=int, help="只看最后 N 条")

    reading = areas.add_parser("reading", help="PDF 阅读器“不懂”标记的难句讲解").add_subparsers(dest="command", required=True)
    command(reading, "list", reading_list, "列出有待讲解标记的文档（--all 列出全部）").add_argument("--all", action="store_true")
    command(reading, "marks", reading_marks, "列出一个文档的全部标记及讲解状态").add_argument("document")
    sub = command(reading, "task", reading_task, "为一个文档的待讲解标记生成讲解任务（截图、前后文、讲义 TeX、规则）")
    sub.add_argument("document", help="文档编号，或标题/路径中能唯一确定文档的片段")
    sub.add_argument("--mark", type=int, action="append", help="只讲这些标记编号，可重复")
    command(reading, "check", reading_check, "只检查 OUTPUT.json，不写入").add_argument("task")
    command(reading, "collect", reading_collect, "检查通过后正式导入讲解（备份、写后回读）").add_argument("task")

    grading = areas.add_parser("grading", help="英文写作批改").add_subparsers(dest="command", required=True)
    sub = command(grading, "prepare", grading_prepare, "生成批改任务（默认用最近一次作答）")
    sub.add_argument("code")
    sub.add_argument("--response", help="作答原文")
    sub.add_argument("--response-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    logged = args.area != "activity"
    _PROGRESS.clear()
    started = datetime.now().astimezone()
    call_id = chatgpt_work_activity.record_start(argv, started) if logged else ""
    try:
        # Libraries print notices (e.g. PyMuPDF's fitz deprecation) to stdout;
        # keep stdout a single JSON object that Work can parse.
        with contextlib.redirect_stdout(sys.stderr):
            result = args.handler(args)
    except Exception as error:  # noqa: BLE001 - reported to ChatGPT Work as JSON
        if logged:
            chatgpt_work_activity.record_call(argv, started, datetime.now().astimezone(), False,
                                              error=str(error), progress=_PROGRESS, call_id=call_id)
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    payload = _jsonable(result)
    if logged:
        chatgpt_work_activity.record_call(argv, started, datetime.now().astimezone(), True,
                                          result=payload, progress=_PROGRESS, call_id=call_id)
    print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
