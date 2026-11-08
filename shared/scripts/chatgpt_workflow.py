"""Resume and setup adapters for Work; existing application services own business writes."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from shared.scripts.ai_agent_operation_registry import FORMAL_WRITE, OperationSpec


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _dashboard(executor: Any) -> Any:
    from shared.scripts.problem_bank_center_qt import DashboardService
    return DashboardService(executor.repository.subject_configs())


def create_subject(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    from shared.scripts import study_project_service as service
    name = args["subject_name"].strip()
    if name in executor.repository.subject_configs():
        return {"subject_name": name, "reused": True, "readback_verified": True}
    # The UI creation service also creates the initial personal learning project.
    # Snapshot its registry before using that same implementation.
    folder, prefix = args["folder_name"], args["prefix"].upper()
    if not folder or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in folder):
        raise ValueError("Use a plain subject folder name, without path components.")
    if not prefix or not prefix.isascii() or not prefix.isalnum():
        raise ValueError("Use a nonempty ASCII alphanumeric subject prefix.")
    registry = service.ensure_subject_registry()
    if any(str(v.get("prefix", "")).upper() == prefix for v in registry.values()):
        raise ValueError("Subject prefix already exists; select the existing subject or another prefix.")
    backup = service.APP_PATHS.user_data_root / "shared/backups" / ("subjects_before_work_create_" + _stamp() + ".json")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(service.SUBJECTS_PATH, backup)
    result = service.create_subject(name, folder, prefix, args.get("domain", "math"))
    if name not in executor.repository.subject_configs():
        raise RuntimeError("Subject registration readback failed; registry backup is " + str(backup))
    return {"subject_name": name, "subject": result, "registry_backup": str(backup),
            "reused": False, "readback_verified": True,
            "next_step": "List projects: subject creation already creates its personal learning project."}


def create_project(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    subject, name = args["subject_name"], args["name"].strip()
    project_type = args.get("collection_type", "personal")
    if not name:
        raise ValueError("Project name cannot be empty.")
    matches = [p for p in executor.repository.list_projects(subject)["projects"] if p["name"] == name]
    if matches:
        if len(matches) != 1 or matches[0]["project_type"] != project_type:
            raise ValueError("An existing project has this name with a different type; do not silently convert it.")
        try:
            directory, _ = executor.repository._project_directory(subject, matches[0]["project_code"])
        except ValueError:
            # Subject creation registers its initial collection before the UI
            # has ever selected it and materialized its existing TeX skeleton.
            service = _dashboard(executor)
            collection = service.collection_detail(subject, matches[0]["project_id"])
            service.ensure_project_latex_skeleton(subject, collection)
            directory, _ = executor.repository._project_directory(subject, matches[0]["project_code"])
        return {"project": matches[0], "project_dir": str(directory), "reused": True, "readback_verified": True}
    service = _dashboard(executor)
    backup_dir = Path(service.cfg(subject)["backups"])
    before = set(backup_dir.glob("*.db"))
    project_id = service.create_collection(subject, name, project_type,
                                            description=args.get("description", ""))
    project = dict(service.collection_detail(subject, project_id))
    directory, _ = executor.repository._project_directory(subject, project["collection_code"])
    return {"project": project, "project_dir": str(directory), "reused": False,
            "database_backups": [str(p) for p in sorted(set(backup_dir.glob("*.db")) - before)],
            "readback_verified": True}


def register_textbook(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    subject = args["subject_name"]
    values = dict(args["values"])
    source = Path(values["pdf_path"]).resolve(strict=True)
    with fitz.open(source) as pdf:
        if not pdf.is_pdf or pdf.needs_pass or not pdf.page_count:
            raise ValueError("The textbook must be a readable, unlocked, nonempty PDF.")
        page_count = pdf.page_count
    source_hash = _digest(source)
    books = executor.repository.list_textbooks(subject)["textbooks"]
    matches = [book for book in books if book["book_code"] == values.get("book_code")
               or (book.get("pdf_path") and Path(book["pdf_path"]).resolve() == source)]
    if matches:
        if len(matches) != 1 or not matches[0].get("pdf_available"):
            raise ValueError("A textbook record already exists; inspect/rebind it rather than creating a duplicate.")
        if _digest(Path(matches[0]["pdf_path"])) != source_hash:
            raise ValueError("The existing textbook code points to a different PDF.")
        return {"textbook": matches[0], "reused": True, "sha256": source_hash,
                "page_count": page_count, "readback_verified": True}
    service = _dashboard(executor)
    values.setdefault("book_code", service.suggested_book_code(subject))
    values["pdf_path"] = str(source)
    backup = service.add_book(subject, values)
    book = next((b for b in executor.repository.list_textbooks(subject)["textbooks"]
                 if b["book_code"] == values["book_code"]), None)
    if book is None or Path(book["pdf_path"]).resolve() != source or _digest(source) != source_hash:
        raise RuntimeError("Textbook registration/PDF binding readback failed; backup is " + str(backup))
    return {"textbook": book, "database_backup": str(backup), "reused": False,
            "sha256": source_hash, "page_count": page_count, "readback_verified": True}


def _subject_folder(executor: Any, subject: str) -> Path:
    configs = executor.repository.subject_configs()
    if subject not in configs:
        raise ValueError("Unknown subject; list subjects and reuse the exact subject name.")
    return Path(configs[subject]["folder"])


def download_reference(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    destination = args.get("destination", "project_references")
    if destination == "subject_textbook":
        # The same folder the control center's textbook registration dialog opens.
        folder = _subject_folder(executor, args["subject_name"]) / "textbook"
    else:
        if not args.get("project_ref"):
            raise ValueError("project_references needs project_ref; use destination=subject_textbook for a subject-wide textbook.")
        directory, _ = executor.repository._project_directory(args["subject_name"], args["project_ref"])
        folder = directory / "references"
    name, url = args["filename"], args["url"]
    if Path(name).name != name or "/" in name or "\\" in name or not name.lower().endswith(".pdf"):
        raise ValueError("Use a plain PDF filename inside the selected references/textbook folder.")
    if urllib.parse.urlparse(url).scheme not in {"https", "http"}:
        raise ValueError("A reference download requires an HTTP(S) source URL.")
    target = folder / name
    manifest = target.with_suffix(".source.json")
    if target.exists():
        if manifest.exists():
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if record.get("url") == url and record.get("sha256") == _digest(target):
                return {**record, "pdf_path": str(target), "reused": True, "readback_verified": True}
        raise ValueError("The reference filename already exists; inspect/reuse it or select a new filename.")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".download_", suffix=".pdf", dir=target.parent)
    os.close(descriptor)
    staging = Path(temporary)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MathProblemBank-reference/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, staging.open("wb") as output:
            final_url = response.geturl()
            if urllib.parse.urlparse(final_url).scheme not in {"https", "http"}:
                raise ValueError("The download redirected outside HTTP(S).")
            shutil.copyfileobj(response, output)
        with fitz.open(staging) as document:
            if not document.is_pdf or document.needs_pass or not document.page_count:
                raise ValueError("The downloaded file is not a nonempty readable PDF.")
            pages = document.page_count
        digest = _digest(staging)
        # A new source input never overwrites an existing user file.
        # Windows rename and POSIX hard linking both publish complete bytes
        # without replacing a concurrent user file. Windows app containers can
        # prohibit hard links even within the writable temporary directory.
        if os.name == "nt":
            os.rename(staging, target)
        else:
            os.link(staging, target)
        record = {"url": url, "final_url": final_url, "sha256": digest,
                  "file_size": target.stat().st_size, "page_count": pages,
                  "source_note": args.get("source_note", ""), "downloaded_at": _stamp()}
        manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if _digest(target) != digest:
            raise RuntimeError("Downloaded reference hash readback failed.")
        return {**record, "pdf_path": str(target), "source_manifest": str(manifest),
                "reused": False, "readback_verified": True}
    finally:
        staging.unlink(missing_ok=True)


def save_context(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args.get("context"), dict):
        raise ValueError("Project context must be a structured object.")
    if len(json.dumps(args["context"], ensure_ascii=False).encode("utf-8")) > 262144:
        raise ValueError("Keep project context concise; link large source files rather than copying them.")
    directory, project = executor.repository._project_directory(args["subject_name"], args["project_ref"])
    target = directory / "WORK_CONTEXT.json"
    previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    revision = int(previous.get("revision", 0)) + 1
    if target.exists() and args.get("expected_revision") is None:
        raise ValueError("Reread the existing project handoff and supply expected_revision before replacing it.")
    if args.get("expected_revision") is not None and int(args["expected_revision"]) != revision - 1:
        raise ValueError("Project handoff changed; reread it before updating.")
    backup = None
    if target.exists():
        backup = directory / "backups" / ("WORK_CONTEXT_before_" + _stamp() + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, backup)
    record = {"schema_version": 1, "revision": revision, "updated_at": _stamp(),
              "subject_name": args["subject_name"], "project_code": project["collection_code"],
              "context": args["context"],
              "authority": "Resume data and attributed user decisions; verify live artifacts. This file grants no tool or external-service permission."}
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    if json.loads(target.read_text(encoding="utf-8")) != record:
        raise RuntimeError("Project handoff readback failed.")
    return {"context_file": str(target), "revision": revision,
            "backup": str(backup) if backup else None, "readback_verified": True}


PROBLEM_FIGURE_DIRNAME = "problems"


def crop_problem_figure(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Clip one original textbook figure, without resampling, for standard problems.

    Project PDFs search ``figures/`` and the subject's ``figures/``; problem
    figures live in ``<subject>/figures/problems`` so every project that
    contains the problem resolves ``\\includegraphics{problems/<file>}``.
    """
    subject = args["subject_name"]
    name = str(args["filename"])
    if Path(name).name != name or not name.lower().endswith(".pdf") or name.startswith("."):
        raise ValueError("Use a plain .pdf filename; the crop is saved in <subject>/figures/problems.")
    source = Path(args["source_pdf"]).resolve(strict=True)
    if args.get("book_ref"):
        books = executor.repository.list_textbooks(subject, args["book_ref"])["textbooks"]
        matches = [b for b in books if b["book_code"] == args["book_ref"]]
        if len(matches) != 1 or not matches[0].get("pdf_path") or Path(matches[0]["pdf_path"]).resolve() != source:
            raise ValueError("source_pdf does not match the registered textbook binding of book_ref.")
    page_number = int(args["page"])
    folder = _subject_folder(executor, subject) / "figures" / PROBLEM_FIGURE_DIRNAME
    target = folder / name
    provenance_file = target.with_suffix(".source.json")
    source_hash = _digest(source)
    with fitz.open(source) as document:
        if not 1 <= page_number <= len(document):
            raise ValueError("page is a physical PDF page number outside the source PDF.")
        rectangle = fitz.Rect(args["bbox"])
        if rectangle.is_empty or not document[page_number - 1].rect.contains(rectangle):
            raise ValueError("bbox (PDF points: left, top, right, bottom) is outside the source page.")
        identity = {"kind": "original_pdf_crop", "source_pdf": str(source), "source_sha256": source_hash,
                    "physical_page": page_number, "bbox": list(rectangle), "resampled": False,
                    "book_ref": args.get("book_ref", ""), "title": args.get("title", "")}
        if target.exists():
            if provenance_file.exists():
                record = json.loads(provenance_file.read_text(encoding="utf-8"))
                same = all(record.get(k) == identity[k] for k in ("source_sha256", "physical_page", "bbox"))
                if same and record.get("sha256") == _digest(target):
                    return {**record, "file": str(target), "includegraphics_path": f"{PROBLEM_FIGURE_DIRNAME}/{name}",
                            "reused": True, "readback_verified": True}
            raise ValueError("A different problem figure already uses this filename; choose another filename.")
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / (".crop_" + _stamp() + ".pdf")
        try:
            with fitz.open() as output:
                page = output.new_page(width=rectangle.width, height=rectangle.height)
                page.show_pdf_page(page.rect, document, page_number - 1, clip=rectangle)
                output.save(temporary, garbage=4, deflate=True)
            # Never replace a concurrently created user file.
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    record = {**identity, "sha256": _digest(target), "created_at": _stamp()}
    provenance_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    with fitz.open(target) as crop:
        if len(crop) != 1 or abs(crop[0].rect.width - rectangle.width) > .01:
            raise RuntimeError("Problem figure readback failed.")
    return {**record, "file": str(target), "provenance_file": str(provenance_file),
            "includegraphics_path": f"{PROBLEM_FIGURE_DIRNAME}/{name}", "reused": False,
            "readback_verified": True}


def _relative(executor: Any, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(executor.repository.root_dir).resolve()).as_posix()
    except ValueError:
        return str(path)


def storage_map(executor: Any, subject: str = "", project_dir: Path | None = None,
                project_code: str = "", courses: list[Any] | None = None) -> dict[str, Any]:
    """Where each kind of file lives, so Work reads and saves in the formal places."""
    rel = lambda path: _relative(executor, Path(path))  # noqa: E731
    scratch = "tmp/work/" + (project_code or "_general")
    result: dict[str, Any] = {
        "path_base": "repository_root",
        "repository_root": str(executor.repository.root_dir),
        "work_scratch": {"path": scratch,
            "use_for": "Work's own request JSONs, page renders it wants to keep briefly, notes; not synced, not formal data"},
        "textbook_caches": {
            "ocr_text": "derived by the textbook index tools (local cache, not synced)",
            "page_images": "render_textbook_pages_for_ai returns each PNG path; cache only"},
        "rules": [
            "Registered textbook PDFs stay where their binding points; never move, rename or copy them to make a new book.",
            "A new subject-wide textbook goes to the subject textbook folder (download_study_reference destination=subject_textbook, or the user's own file there).",
            "Project-specific downloads, lecture notes, web pages and source manifests go to the project references folder.",
            "Course reference materials (MinerU text/formulas/figures) are created by import_online_course_reference_materials inside the course storage; do not hand-copy them.",
            "Write only the output file named by a task (OUTPUT.tex, IMPORT.txt, ...); figures for standard problems only through crop_problem_textbook_figure.",
            "Request JSON files and other scratch go under work_scratch, never the repository root or a subject/project folder.",
        ],
    }
    if subject:
        cfg = executor.repository.subject_configs().get(subject)
        if cfg:
            folder = Path(cfg["folder"])
            result["subject"] = {
                "folder": rel(folder),
                "textbook_dir": rel(folder / "textbook"),
                "problem_figures_dir": rel(folder / "figures" / PROBLEM_FIGURE_DIRNAME),
                "problem_figure_tex_path": PROBLEM_FIGURE_DIRNAME + "/<file>.pdf",
                "problem_import_task_dir": rel(Path(cfg["exports"]) / "chatgpt_work_import"),
                "database": rel(cfg["db"]) + " (read/write only through tools and task commands)",
                "backups": rel(cfg["backups"]),
            }
    if project_dir is not None:
        result["project"] = {
            "folder": rel(project_dir),
            "references_dir": rel(project_dir / "references"),
            "handoff_file": rel(project_dir / "WORK_CONTEXT.json"),
            "figures_dir": rel(project_dir / "figures") + " (project cover and project-only figures)",
            "main_tex": rel(project_dir / "main.tex") + " (generated; do not edit)",
        }
    if courses:
        result["courses"] = []
        for row in courses:
            storage = Path(row["storage_dir"])
            result["courses"].append({
                "course_id": int(row["id"]), "course_code": row["course_code"],
                "storage_dir": rel(storage),
                "formal_pdf": rel(storage / (row["course_code"] + ".pdf")),
                "reference_sources": rel(storage / "reference_materials" / "sources"),
                "reference_parts": rel(storage / "reference_materials" / "parts") + "/material_<id>/part_*.md|pdf, assets/ (MinerU output)",
                "textbook_catalog": rel(storage / "reference_materials" / "TEXTBOOK_CATALOG.json") + " (all imported books, original figures, Work semantic groups and topic map; prepare_online_course_textbook_catalog)",
                "episode_materials": rel(storage / "episodes") + "/episode_<n>/ (recording evidence, material ZIP)",
                "permanent_source_archives": rel(storage / "evidence_archives") + "/episode_<n>/raw_source_<package_sha256>.json (original ASR/captions/provenance; raw media still expires)",
                "episode_writing_tasks": rel(storage / "episode_tasks") + "/episode_<nnn>/ (whole-episode TASK.md, OUTPUT.tex; course episode-task)",
                "writing_tasks": rel(storage / "subsections") + "/subsection_<key>/chatgpt_package/ (single-subsection TASK.md, OUTPUT.tex)",
                "lecture_latex": rel(storage / "latex") + " (formal; changed only by course collect)",
                "lecture_textbook_figures": rel(storage / "latex" / "textbook_figures"),
            })
    return result


def bootstrap(executor: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Bounded metadata only; do not OCR, run a model or prepare new packages."""
    subject, project_ref = args.get("subject_name", ""), args.get("project_ref", "")
    result: dict[str, Any] = {
        "entry_files": ["AGENTS.md", ".agents/skills/mathproblembank-workflow/SKILL.md", "docs/CHATGPT_WORK_GUIDE.zh-CN.md"],
        "model_calls": 0, "policy": {"independent_math_audit": "off", "textbook_original_figures": "required_for_assigned_topics",
                                    "math_lecture_reference_reading": "Before drafting, ChatGPT Work compares relevant textbooks "
                                    "and checks the reading and beginner teaching plan in SOURCES.json (guide 3.0b)."},
    }
    if not subject:
        result["subjects"] = executor.repository.list_subjects()
        result["storage"] = storage_map(executor)
        result["next_action"] = "Select/reuse the subject from the user request; create it only if absent."
        return result
    result["projects"] = executor.repository.list_projects(subject)
    result["textbooks"] = executor.repository.list_textbooks(subject)
    if not project_ref:
        result["storage"] = storage_map(executor, subject)
        result["next_action"] = "Resolve the requested project, reusing the personal project created with the subject."
        return result
    try:
        directory, project = executor.repository._project_directory(subject, project_ref)
    except ValueError:
        matches = [p for p in result["projects"]["projects"] if str(project_ref) in
                   {str(p["project_id"]), p["project_code"], p["name"]}]
        if len(matches) != 1:
            raise
        result["project"] = matches[0]
        result["storage"] = storage_map(executor, subject)
        result["next_action"] = "The project is registered but its directory is not prepared; use create_study_project to reuse it and prepare the existing skeleton."
        return result
    result["project"] = {"code": project["collection_code"], "type": project["collection_type"], "directory": str(directory)}
    context = directory / "WORK_CONTEXT.json"
    result["handoff"] = json.loads(context.read_text(encoding="utf-8")) if context.exists() else None
    result["source_inputs"] = [{"path": str(p), "bytes": p.stat().st_size}
                               for p in sorted((directory / "references").glob("*.pdf"))]
    courses = executor.online_courses.courses_for_project(subject, project["collection_code"])
    selected = int(args.get("course_id") or 0)
    if selected and not any(int(c["id"]) == selected for c in courses):
        raise ValueError("The requested course does not belong to this project.")
    result["courses"] = []
    for row in courses:
        item = {k: row[k] for k in ("id", "course_code", "title", "course_mode", "status")}
        if selected == int(row["id"]):
            item["reference_materials"] = executor.online_courses.reference_materials(selected)
            item["course_reference_state"] = ("inspect_material_status_and_manifests" if item["reference_materials"]
                                              else "missing_course_reference_import")
            item["units"] = []
            # Reading progress must not run course_subsections' label-repair path.
            with executor.online_courses.connect(rows=True) as connection:
                units = connection.execute(
                    "SELECT * FROM course_subsections WHERE course_id=? ORDER BY stable_key", (selected,)
                ).fetchall()
            for unit in units:
                task = executor.online_courses.chatgpt_work_task(int(unit["id"]))
                item["units"].append({"subsection_id": unit["id"], "stable_key": unit["stable_key"],
                    "title": unit["subsection_title"], "task_ready": task["task_ready"],
                    "task_file": task["task_file"], "draft_exists": task["draft_exists"],
                    "writing_materials_status": task.get("writing_materials_status", "needs_one_time_preparation"),
                    "primary_writing_material": task.get("primary_writing_material", "")})
            pdf = Path(row["storage_dir"]) / (row["course_code"] + ".pdf")
            item["pdf_path"], item["pdf_exists"] = str(pdf), pdf.is_file()
        result["courses"].append(item)
    result["storage"] = storage_map(executor, subject, directory, project["collection_code"], courses)
    allowed_roots = [directory.resolve(), *[Path(c["storage_dir"]).resolve() for c in courses]]
    result["artifact_readback"] = []
    for artifact in ((result.get("handoff") or {}).get("context") or {}).get("artifacts", [])[:100]:
        if not isinstance(artifact, dict) or not artifact.get("path"):
            continue
        path = Path(artifact["path"])
        base = executor.repository.root_dir if artifact.get("path_base") == "repository" else directory
        path = (path if path.is_absolute() else base / path).resolve()
        item = {"path": str(path)}
        if not any(root == path or root in path.parents for root in allowed_roots):
            item["status"] = "outside_project_scope_not_read"
        elif not path.is_file():
            item["status"] = "missing"
        else:
            item["bytes"] = path.stat().st_size
            item["status"] = ("hash_matches" if _digest(path) == artifact["sha256"] else "hash_changed") if artifact.get("sha256") else "exists"
        result["artifact_readback"].append(item)
    result["next_action"] = "Compare handoff claims with live records, then resume the first unfinished requested stage; OCR indexing and MinerU course-reference parsing are separate stages."
    return result


_SUBJECT = {"subject_name": {"type": "string", "minLength": 1}}
_PROJECT = {**_SUBJECT, "project_ref": {"type": ["string", "integer"]}}
_BOOK_FIELDS = {key: {"type": "string"} for key in
                ("book_code", "title", "author", "edition", "publisher", "notes", "isbn", "external_url", "pdf_path")}
_BOOK_FIELDS["publication_year"] = {"type": ["integer", "null"]}


def _spec(name: str, handler: str, properties: dict, required: list[str], *, write: bool = True) -> OperationSpec:
    return OperationSpec(operation_id="work.workflow." + name, tool_name=name,
        description={
            "get_workflow_context": "读取项目交接记录、正式教材绑定、课程及任务进度；不运行OCR、MinerU或模型。",
            "create_study_subject": "通过现有学科服务创建学科（含默认学习问题集），备份登记并回读；已有学科复用。",
            "create_study_project": "通过现有控制中心服务创建学习项目并回读；默认personal，不因有教材改成习题集。",
            "register_study_textbook": "通过教材登记服务登记教材并绑定已有PDF，保留备份、哈希及回读；重复PDF复用。",
            "download_study_reference": "将明确选定的公开HTTP(S) PDF保存到项目references（默认）或学科textbook文件夹，校验可读性、来源和哈希；不覆盖已有文件。",
            "crop_problem_textbook_figure": "把教材PDF一页上的一幅原图按PDF坐标无重采样裁切到学科figures/problems，供标准题以\\includegraphics{problems/文件名}引用，并记录来源哈希；不覆盖已有文件。",
            "save_workflow_context": "备份并保存项目目标、用户决定、覆盖记录与待办，供新对话续接；不授予外部调用权限。",
        }[name], parameters={"type": "object", "properties": properties,
                             "required": required, "additionalProperties": False},
        handler_name=handler, access_level=FORMAL_WRITE if write else "read_only",
        category="work_workflow", evidence_policy="existing_service_backup_and_formal_record_readback" if write else "bounded_live_metadata_no_model_calls")


_EPISODE = {"type": "object", "properties": {"course_id": {"type": "integer", "minimum": 1},
            "episode_number": {"type": "integer", "minimum": 1}},
            "required": ["course_id", "episode_number"], "additionalProperties": False}

WORKFLOW_OPERATION_SPECS = (
    OperationSpec(
        operation_id="online_course.episode_intake.prepare",
        tool_name="prepare_online_course_episode_intake",
        description="把刚录好的一集交给 Work：写出 Groq 转写、带编号的候选帧缩略图、教材目录和已有小节，由 Work 直接从截图提取数学内容并决定分节和教材；不调用模型。",
        parameters=_EPISODE, handler_name="_tool_prepare_online_course_episode_intake",
        access_level="derived_write", category="online_course_materials",
        evidence_policy="local_transcript_and_candidate_frames_no_model_calls"),
    OperationSpec(
        operation_id="online_course.episode_intake.apply",
        tool_name="apply_online_course_episode_intake",
        description="读取 Work 写的 INTAKE.json，校验分节和教材后用现有的窗口锁定、目录和材料包流程生成这一集的材料和小节任务；不调用模型。",
        parameters=_EPISODE, handler_name="_tool_apply_online_course_episode_intake",
        access_level=FORMAL_WRITE, category="online_course_materials",
        evidence_policy="validated_work_decisions_through_existing_package_flow_with_readback"),
    OperationSpec(
        operation_id="online_course.episode_writing_task.prepare",
        tool_name="prepare_online_course_episode_writing_task",
        description="把一集的全部已就绪小节写作任务合成一个整集任务：共享规则和往期讲义各一份，每节只保留其范围合同、连续写作底稿和回查证据；不调用模型、不改锁定证据。",
        parameters=_EPISODE,
        handler_name="_tool_prepare_online_course_episode_writing_task",
        access_level="derived_write", category="online_course_materials",
        evidence_policy="copied_from_ready_subsection_tasks_with_hash_checked_shared_rules"),
    OperationSpec(
        operation_id="online_course.reference_reading.check",
        tool_name="check_online_course_reference_reading",
        description="ChatGPT Work 动笔前核对整集或单节 SOURCES.json 的教材阅读与写作准备。检查候选资料是否已交代、原页锚点是否存在。无需 OUTPUT.tex，不调用模型。",
        parameters={
            "type": "object",
            "properties": {**_EPISODE["properties"], "subsection_id": {"type": "integer", "minimum": 1}},
            "oneOf": [{"required": ["subsection_id"], "not": {"anyOf": [
                {"required": ["course_id"]}, {"required": ["episode_number"]}]}},
                {"required": ["course_id", "episode_number"], "not": {"required": ["subsection_id"]}}],
            "additionalProperties": False,
        },
        handler_name="_tool_check_online_course_reference_reading", access_level="read_only",
        category="online_course_materials", evidence_policy="local_candidate_coverage_and_page_anchors",
    ),
    OperationSpec(
        operation_id="online_course.episode_writing_output.check",
        tool_name="check_online_course_episode_writing_output",
        description="按小节标记拆分整集 OUTPUT.tex，并用各小节正式导入的本地检查逐节校验；不写入。",
        parameters=_EPISODE, handler_name="_tool_check_online_course_episode_writing_output",
        access_level="read_only", category="online_course_materials",
        evidence_policy="per_subsection_deterministic_import_precheck"),
    OperationSpec(
        operation_id="online_course.episode_writing_output.import",
        tool_name="import_online_course_episode_writing_output",
        description="先逐节检查整集 OUTPUT.tex，全部通过后逐节正式导入（各自备份和回读，不编译），最后整门课程只重建和编译一次 PDF。",
        parameters=_EPISODE, handler_name="_tool_import_online_course_episode_writing_output",
        access_level=FORMAL_WRITE, category="online_course_materials",
        evidence_policy="all_units_prechecked_then_existing_imports_and_single_full_rebuild"),
    OperationSpec(
        operation_id="online_course.reference_pages.recover",
        tool_name="save_online_course_page_recoveries",
        description="保存Work实际查看原页后对MinerU空识别页的逐页转写补充，明确标记Work来源并保留原始MinerU检查点；不覆盖已有非空内容，不调用模型。随后使用既有参考资料重分析入口复用缓存并应用补充，不重录或重跑全书识别。",
        parameters={"type":"object", "properties": {
            "course_id":{"type":"integer","minimum":1}, "material_id":{"type":"integer","minimum":1},
            "source_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
            "pages":{"type":"array","minItems":1,"maxItems":30,"items":{"type":"object",
                "properties":{"page_number":{"type":"integer","minimum":1},
                              "content":{"type":"string","minLength":32,"maxLength":60000},
                              "visual_evidence":{"type":"string","minLength":1},
                              "quality_notes":{"type":"string"}},
                "required":["page_number","content","visual_evidence"],"additionalProperties":False}}},
            "required":["course_id","material_id","source_sha256","pages"],"additionalProperties":False},
        handler_name="_tool_save_online_course_page_recoveries", access_level="derived_write",
        category="online_course_materials", evidence_policy="unchanged_source_empty_page_only_labelled_overlay_history_and_readback",
    ),
    OperationSpec(
        operation_id="online_course.textbook_catalog.prepare",
        tool_name="prepare_online_course_textbook_catalog",
        description="从课程已导入的全部教材节、原图和来源哈希建立可复用总目录；可保存Work亲自阅读后的章节质量、跨书图意分组和主题映射。无模型调用，不重跑MinerU或录制，不改正式讲义。未完成实际阅读的资料明确标为未复核。",
        parameters={"type": "object", "properties": {
            "course_id": {"type": "integer", "minimum": 1},
            "preview_figure_ids": {"type": "array", "items":{"type":"string"}, "maxItems":4,
                                   "description":"最多4幅已定位候选的原页裁图预览；返回页码、来源校验与PNG视觉证据。"},
            "discover_inline_diagrams": {"type":"boolean", "description":"只从已有完整MinerU缓存补发现行内交换图等候选，并裁取原PDF；不重跑识别、不调用模型、不改稳定节目录。"},
            "review_file": {"type": "string", "description": "Work阅读后填写的JSON；必须带当前source_fingerprint，保留来源、结构/质量说明和图意分组。"}},
            "required": ["course_id"], "additionalProperties": False},
        handler_name="_tool_prepare_online_course_textbook_catalog",
        access_level="derived_write", category="online_course_materials",
        evidence_policy="source_and_section_hashes_verified_original_crops_writer_review_atomic_readback",
    ),
    _spec("get_workflow_context", "_tool_get_workflow_context", {**_PROJECT, "course_id": {"type": "integer", "minimum": 1}}, [], write=False),
    _spec("create_study_subject", "_tool_create_study_subject", {**_SUBJECT,
        "folder_name": {"type": "string"}, "prefix": {"type": "string"},
        "domain": {"type": "string", "enum": ["math", "physics"]}}, ["subject_name", "folder_name", "prefix"]),
    _spec("create_study_project", "_tool_create_study_project", {**_SUBJECT,
        "name": {"type": "string"}, "collection_type": {"type": "string", "enum": ["personal", "textbook", "custom"]},
        "description": {"type": "string"}}, ["subject_name", "name"]),
    _spec("register_study_textbook", "_tool_register_study_textbook", {**_SUBJECT,
        "values": {"type": "object", "properties": _BOOK_FIELDS,
                   "required": ["title", "pdf_path"], "additionalProperties": False}}, ["subject_name", "values"]),
    _spec("download_study_reference", "_tool_download_study_reference", {**_PROJECT,
        "url": {"type": "string"}, "filename": {"type": "string"}, "source_note": {"type": "string"},
        "destination": {"type": "string", "enum": ["project_references", "subject_textbook"]}},
        ["subject_name", "url", "filename"]),
    _spec("crop_problem_textbook_figure", "_tool_crop_problem_textbook_figure", {**_SUBJECT,
        "source_pdf": {"type": "string"}, "page": {"type": "integer", "minimum": 1},
        "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "filename": {"type": "string"}, "title": {"type": "string"}, "book_ref": {"type": "string"}},
        ["subject_name", "source_pdf", "page", "bbox", "filename"]),
    _spec("save_workflow_context", "_tool_save_workflow_context", {**_PROJECT,
        "context": {"type": "object"}, "expected_revision": {"type": "integer", "minimum": 0}},
        ["subject_name", "project_ref", "context"]),
)
