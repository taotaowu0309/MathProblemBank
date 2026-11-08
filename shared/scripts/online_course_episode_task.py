"""Whole-episode writing task for ChatGPT Work, assembled from the subsection tasks.

The existing per-subsection packages stay the formal evidence and import units.
This module only copies, once per episode, what a writer actually reads: one copy
of the shared permanent contracts, one copy of the prior lecture PDF, and for each
subsection its own contract, continuous writing materials and lookup evidence.
Work writes all subsection bodies into one ``OUTPUT.tex``; collection validates
every body first, imports each without recompiling, and rebuilds the course once.
No model is called.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from shared.scripts import online_course_source_ledger as source_ledger
from shared.scripts.online_course_work_intake import intake_dir

TASK_FILENAME = "TASK.md"
OUTPUT_FILENAME = "OUTPUT.tex"
MANIFEST_FILENAME = "EPISODE_TASK.json"
SCHEMA_VERSION = 1

# Identical in every subsection package; read once per episode.
SHARED_RULE_FILES = (
    "GLOBAL_CHATGPT_WRITING_RULES.txt",
    "LATEX_IMPORT_CONTRACT.txt",
    "LATEX_DRAWING_RULES.txt",
    "REFERENCE_AND_CONTINUITY_PROMPT.txt",
    "UNEXPANDED_PROOFS_PROMPT.txt",
)
# Read completely for every subsection.
UNIT_PRIMARY_FILES = ("WHOLE_SUBSECTION_WRITING_PROMPT.txt", "WRITING_MATERIALS.md")
# Source archives: consulted at precise anchors when something is uncertain.
UNIT_LOOKUP_FILES = (
    "AUDIO_TRANSCRIPT.md", "BOARD_NOTES.md", "BOARD_KEYFRAMES.md",
    "LECTURE_ERRORS_AND_CORRECTIONS.md", "EVIDENCE_OBLIGATIONS.json",
)
UNIT_LOOKUP_DIRS = ("figures", "reference_materials")
CONTINUITY_FILES = (
    "COMPLETE_LECTURE_NOTES.pdf", "THEOREM_AND_NOTATION_REGISTRY.json",
    "PREVIOUS_NOTES_INDEX.json", "CONTINUITY_MANIFEST.json",
)
MARKER = re.compile(r"(?m)^%%%\s*BEGIN\s+SUBSECTION\s+(\d+)\b[^\n]*$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    if path.read_text(encoding="utf-8") != text:
        raise RuntimeError(f"写后回读失败：{path}")


def episode_task_dir(service: Any, course_id: int, episode_number: int) -> Path:
    # Not under episodes/: the retention job removes temporary episode evidence there.
    storage = Path(str(service.course(int(course_id))["storage_dir"]))
    return storage / "episode_tasks" / f"episode_{int(episode_number):03d}"


def episode_units(service: Any, course_id: int, episode_number: int, *,
                  repair_missing_labels: bool = True) -> list[dict[str, Any]]:
    """Recorded subsections whose locked evidence includes this episode, in outline order."""
    units = []
    for row in service.course_subsections(int(course_id), repair_missing_labels=repair_missing_labels):
        subsection_id = int(row["id"])
        task_dir = service.subsection_task_dir(subsection_id)
        manifest_path = task_dir / "package_manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        segments = [s for s in manifest.get("source_segments", []) if not s.get("deduplicated_fully")]
        if not any(int(s.get("episode_number") or 0) == int(episode_number) for s in segments):
            continue
        task = service.chatgpt_work_task(subsection_id)
        units.append({
            "subsection_id": subsection_id,
            "stable_key": str(row["stable_key"]),
            "number": str(manifest.get("display_number") or ""),
            "title": str(manifest.get("display_title") or row["subsection_title"] or ""),
            "task_dir": task_dir,
            "task_ready": bool(task["task_ready"]),
            "writing_materials_status": task.get("writing_materials_status", ""),
            "writing_materials_error": task.get("writing_materials_error", ""),
            "other_episodes": sorted({int(s.get("episode_number") or 0) for s in segments} - {int(episode_number)}),
            "lecture_seconds": sum(
                max(0.0, float(s.get("end_video_time") or 0) - float(s.get("start_video_time") or 0))
                for s in segments if int(s.get("episode_number") or 0) == int(episode_number)),
        })
    return units


def _transcript_only_lines(units: list[dict[str, Any]]) -> list[str]:
    """Warn about recording windows whose board was never read; old packages report none."""
    seen: dict[tuple[int, int], str] = {}
    for unit in units:
        try:
            manifest = json.loads((unit["task_dir"] / "package_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for package in manifest.get("source_episode_packages") or []:
            try:
                with zipfile.ZipFile(str(package["path"])) as archive:
                    source = json.loads(archive.read("package_manifest.json").decode("utf-8"))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                continue
            for session in (source.get("recording_agent_windows") or {}).values():
                for window in session.get("transcript_only_windows") or []:
                    start, end = int(window["start_video_time"]), int(window["end_video_time"])
                    seen[(int(package.get("episode_number") or 0), start)] = (
                        f"episode {package.get('episode_number')}, "
                        f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}")
    if not seen:
        return []
    return ["", "BOARD NOT READ. In these ranges the board was not read, so the record rests on speech alone.",
            "Formulas there may be wrong; view the candidate frames you need and check the textbook:",
            *[f"- {label}" for _key, label in sorted(seen.items())]]


def _keyframe_lines(units: list[dict[str, Any]]) -> list[str]:
    lines = []
    for index, unit in enumerate(units, 1):
        folder = unit["task_dir"] / "figures"
        count = sum(1 for path in folder.rglob("*") if path.is_file()) if folder.is_dir() else 0
        lines.append(f"   `units/{index:02d}_{unit['stable_key']}/figures/` ({count} images)")
    return lines


def _mapped_parts(task_dir: Path) -> list[dict[str, Any]]:
    """The textbook parts the application mapped to this subsection."""
    index = task_dir / "reference_materials" / "REFERENCE_INDEX.json"
    if not index.is_file():
        return []
    return list(json.loads(index.read_text(encoding="utf-8")).get("parts") or [])


def _page_span(pages: list[Any]) -> str:
    return f"{pages[0]}–{pages[-1]}" if pages else "?"


def _textbook_reading(course: Any, units: list[dict[str, Any]]) -> list[str]:
    """Name the exact mapped parts, so the corpus is read rather than remembered."""
    guide = Path(str(course["storage_dir"])) / "reference_materials" / "TEXTBOOK_GUIDE.md"
    lines = [
        "3. The textbooks, before planning the text. Read every mapped part below completely, and the",
        "   original page for any formula, proof step or figure you use. View the figures you intend to use.",
    ]
    for index, unit in enumerate(units, 1):
        lines.append(f"   Subsection {unit['number'] or unit['stable_key']}:")
        parts = _mapped_parts(unit["task_dir"])
        folder = f"units/{index:02d}_{unit['stable_key']}"  # content_file is relative to the unit folder
        lines += [f"   - {part.get('source_filename', '')}, pages {_page_span(part.get('page_numbers') or [])}: "
                  f"{part.get('title', '')} — `{folder}/{part.get('content_file', '')}`" for part in parts]
        if not parts:
            lines.append("   - No part is mapped yet. Locate this topic in the primary textbook through the guide.")
    if guide.is_file():
        lines.append(f"   Books by topic, including anything the mapping missed: `{guide}` (absolute path).")
    return lines


def _task_text(course: Any, episode_number: int, units: list[dict[str, Any]], prior_pdf: bool,
               inventory: list[str], materials: list[dict[str, Any]] | None = None,
               board_sheets: Path | None = None) -> str:
    lines = [
        "CHATGPT WORK WHOLE-EPISODE TASK",
        "",
        "Every path in this file is relative to the folder that contains this `TASK.md`.",
        "",
        f"Course {course['course_code']} — episode {int(episode_number)}. Write the complete lecture",
        f"notes of the {len(units)} subsections below into the single file `OUTPUT.tex`, in this order.",
        "Begin each subsection body with its marker line exactly as written here; put nothing",
        "before the first marker. The application splits the file at the markers, checks every",
        "body against its own subsection contract, imports them, and compiles the course once.",
        (f"Also write `{source_ledger.SOURCES_FILENAME}` as described below. Do not create, modify, or "
         "delete any other file." if materials else "Do not create, modify, or delete any other file."),
        "",
        "SUBSECTIONS AND MARKERS",
        "",
    ]
    for index, unit in enumerate(units, 1):
        lines.append(f"{index}. `%%% BEGIN SUBSECTION {unit['subsection_id']} {unit['number']} {unit['title']}`"
                     f" — folder `units/{index:02d}_{unit['stable_key']}/`")
    lines += [
        "",
        "READ ORDER (read each item once for the whole episode)",
        "",
        "1. `rules/` — the permanent contracts shared by every subsection, read completely once:",
        *[f"   `rules/{name}`" for name in SHARED_RULE_FILES],
        "2. For every subsection, read completely its `WHOLE_SUBSECTION_WRITING_PROMPT.txt` (its exact",
        *(["   scope) and its `AUDIO_TRANSCRIPT.md` in full. The transcript has speech-recognition errors; use it for",
           "   the teacher's explanation and correct it from context. The mathematics on the board comes straight",
           "   from the program's screenshots, which nothing has transcribed or filtered. They are on the numbered",
           f"   sheets in `{board_sheets}` (absolute path), and `FRAMES.json` beside that folder has each original.",
           "   Take every definition, statement, formula and diagram from them. You read them during intake; in a",
           "   new conversation read them again. Redraw any board diagram you need; screenshots are not figures.",
           "   For a result that appears only on the board, `lecture_anchor` quotes the teacher's words about it.",
           ] if board_sheets else ["   scope), and, because this episode has no teacher-only draft, its",
           "   `BOARD_NOTES.md` and `AUDIO_TRANSCRIPT.md` in full (the notes may only list the frames kept).",
           "   The transcript has speech-recognition errors;",
           "   use it for the teacher's explanation and correct it from context. View every selected keyframe",
           "   image in its `figures/` folder:"]),
        *([] if board_sheets else _keyframe_lines(units)),
        *(_textbook_reading(course, units) if materials else [
            "3. The textbook: read the primary-textbook pages for this episode's topics (and any",
            "   `reference_materials/` excerpts), and actually view the figures you intend to use.",
        ]),
    ]
    if prior_pdf:
        lines += [
            "4. `continuity/COMPLETE_LECTURE_NOTES.pdf` once, completely, for notation, numbering and",
            "   earlier results, with `continuity/THEOREM_AND_NOTATION_REGISTRY.json`. Reading it once",
            "   satisfies the continuity read of every subsection in this task. An older version of",
            "   this episode inside that PDF is never copied.",
        ]
    lines += [
        "",
        "LOOKUP ONLY (not full reads): each subsection's `AUDIO_TRANSCRIPT.md`, `BOARD_NOTES.md`,",
        "`BOARD_KEYFRAMES.md`, `LECTURE_ERRORS_AND_CORRECTIONS.md` and",
        "`EVIDENCE_OBLIGATIONS.json`: open them at the source anchors in WRITING_MATERIALS.md",
        "when a statement, symbol or figure is uncertain. `latex_context/` shows the macros.",
        "",
        "WRITING",
        "",
        "Plan the episode as one continuous teaching text, then write it. Each subsection body",
        "follows `rules/LATEX_IMPORT_CONTRACT.txt` (body only, no sectioning commands) and stays",
        "inside its own subsection scope; later subsections may refer to results of earlier ones.",
        "Inside a subsection body, file paths (verified textbook crops, figures) are relative to that",
        "subsection's own `units/NN_.../` folder, exactly as in a single-subsection task, e.g.",
        "`reference_materials/excerpt_<id>/figure_01.pdf`.",
        "Expand every argument or calculation the lecture skipped or sketched into a complete",
        "textbook-level proof, integrate the corresponding textbook content, and keep the",
        "teacher's motivation and warnings. The writer checks the mathematics; no other reviewer",
        "is called.",
        "",
        "Do not write exercises or exercise solutions: the user requests them separately. Results the",
        "teacher proved in class stay in the text, as ordinary statements and proofs.",
    ]
    lines += _transcript_only_lines(units)
    if materials:
        reading = source_ledger.reading_applies(course)
        if reading:
            lines += ["", source_ledger.reading_policy_text(), "", "Candidates by subsection:"]
            for unit in units:
                candidates = source_ledger.reading_candidates(materials, unit["task_dir"])
                lines.append(f"- {unit['number']}: material IDs {', '.join(map(str, candidates))}")
        lines += ["", source_ledger.writing_policy_text(), "",
                  source_ledger.record_format_text(materials, [int(u["subsection_id"]) for u in units],
                                                   reading=reading)]
    if any(u["other_episodes"] for u in units):
        lines += ["", "Some subsections also contain evidence from other episodes; their folders include it."]
    lines += ["", "FILE INVENTORY", "", *[f"- `{name}`" for name in inventory]]
    return "\n".join(lines) + "\n"


def prepare_episode_task(service: Any, course_id: int, episode_number: int) -> dict[str, Any]:
    """Assemble the writing task of a whole episode."""
    course = service.course(int(course_id))
    units = episode_units(service, course_id, episode_number)
    if not units:
        raise ValueError("这一集还没有已生成材料的正式小节；先查 get_online_course_processing_status。")
    not_ready = [u["number"] or u["stable_key"] for u in units if not u["task_ready"]]
    if not_ready:
        raise ValueError("这些小节的写作任务还没就绪：" + "、".join(not_ready)
                         + "。先用 course task / course prepare 生成单个小节任务。")
    shared: dict[str, Path] = {}
    for name in SHARED_RULE_FILES:
        copies = [u["task_dir"] / name for u in units if (u["task_dir"] / name).is_file()]
        if not copies:
            raise ValueError(f"小节任务缺少规则文件 {name}；先重新生成小节任务。")
        if len({_sha256(p) for p in copies}) != 1:
            raise ValueError(f"各小节的 {name} 不一致；先重新生成这一集的小节任务再合成整集任务。")
        shared[name] = copies[0]

    target = episode_task_dir(service, course_id, episode_number)
    building = target.with_name(target.name + ".building")
    if building.exists():
        shutil.rmtree(building)
    building.mkdir(parents=True)
    # Work's own draft and archive survive a rebuild of the derived folder.
    for keep in (OUTPUT_FILENAME, source_ledger.SOURCES_FILENAME, "imported_drafts"):
        source = target / keep
        if source.is_dir():
            shutil.copytree(source, building / keep)
        elif source.is_file():
            shutil.copy2(source, building / keep)

    def copy(source: Path, relative: str) -> None:
        destination = building / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)

    for name, source in shared.items():
        copy(source, f"rules/{name}")
    first = units[0]["task_dir"]
    prior_pdf = False
    for name in CONTINUITY_FILES:
        if (first / "continuity" / name).is_file():
            copy(first / "continuity" / name, f"continuity/{name}")
            prior_pdf = prior_pdf or name == "COMPLETE_LECTURE_NOTES.pdf"
    if (first / "latex_context").is_dir():
        copy(first / "latex_context", "latex_context")
    unit_records = []
    for index, unit in enumerate(units, 1):
        folder = f"units/{index:02d}_{unit['stable_key']}"
        for name in (*UNIT_PRIMARY_FILES, *UNIT_LOOKUP_FILES):
            if (unit["task_dir"] / name).is_file():
                copy(unit["task_dir"] / name, f"{folder}/{name}")
        for name in UNIT_LOOKUP_DIRS:
            if (unit["task_dir"] / name).is_dir():
                copy(unit["task_dir"] / name, f"{folder}/{name}")
        related = unit["task_dir"] / "continuity" / "RELATED_PREVIOUS_SUBSECTIONS.tex"
        if related.is_file():
            copy(related, f"{folder}/RELATED_PREVIOUS_SUBSECTIONS.tex")
        unit_records.append({key: unit[key] for key in (
            "subsection_id", "stable_key", "number", "title", "writing_materials_status", "other_episodes")}
            | {"folder": folder})

    inventory = sorted(p.relative_to(building).as_posix() for p in building.rglob("*")
                       if p.is_file() and p.name not in (OUTPUT_FILENAME, source_ledger.SOURCES_FILENAME)
                       and "imported_drafts" not in p.parts)
    inventory = [TASK_FILENAME, MANIFEST_FILENAME, *inventory]
    materials = (source_ledger.source_materials(service, course_id)
                 if any(source_ledger.ledger_required(service, u["subsection_id"]) for u in units) else [])
    sheets = intake_dir(service, course_id, episode_number) / "sheets"
    _write_text(building / TASK_FILENAME, _task_text(course, episode_number, units, prior_pdf, inventory, materials,
                                                       sheets if sheets.is_dir() else None))
    manifest = {"schema_version": SCHEMA_VERSION, "course_id": int(course_id),
                "course_code": str(course["course_code"]), "episode_number": int(episode_number),
                "units": unit_records, "generated_at": datetime.now().isoformat(timespec="seconds"),
                "model_calls": 0}
    _write_text(building / MANIFEST_FILENAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    for name in inventory:
        if not (building / name).is_file():
            raise RuntimeError(f"整集任务文件缺失：{name}")
    if target.exists():
        retired = target.with_name(target.name + ".old")
        if retired.exists():
            shutil.rmtree(retired)
        os.replace(target, retired)
        os.replace(building, target)
        shutil.rmtree(retired)
    else:
        os.replace(building, target)
    return episode_task_status(service, course_id, episode_number)


def episode_task_status(service: Any, course_id: int, episode_number: int) -> dict[str, Any]:
    target = episode_task_dir(service, course_id, episode_number)
    manifest_path = target / MANIFEST_FILENAME
    output = target / OUTPUT_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    text_chars = sum(p.stat().st_size for p in target.rglob("*")
                     if p.is_file() and p.suffix in {".md", ".txt", ".json", ".tex"}
                     and p.name != OUTPUT_FILENAME and "imported_drafts" not in p.parts) if target.is_dir() else 0
    return {"course_id": int(course_id), "episode_number": int(episode_number),
            "task_dir": str(target), "task_file": str(target / TASK_FILENAME),
            "output_file": str(output), "task_ready": (target / TASK_FILENAME).is_file(),
            "draft_exists": output.is_file() and bool(output.read_text(encoding="utf-8").strip()),
            "units": manifest.get("units", []), "text_bytes": text_chars,
            "task_line": f"请完整阅读 {target / TASK_FILENAME} 并按其中要求把整集讲义写进同一文件夹的 OUTPUT.tex。"}


def split_output(text: str, units: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    matches = list(MARKER.finditer(text))
    if not matches:
        raise ValueError("OUTPUT.tex 里没有 `%%% BEGIN SUBSECTION <编号>` 标记行；按 TASK.md 给每个小节加标记。")
    if text[:matches[0].start()].strip():
        raise ValueError("第一个小节标记之前不能有内容。")
    found = [int(m.group(1)) for m in matches]
    expected = [int(u["subsection_id"]) for u in units]
    if found != expected:
        raise ValueError(f"小节标记必须恰好按顺序出现一次：应为 {expected}，实际为 {found}。")
    pieces = []
    for index, (match, unit) in enumerate(zip(matches, units)):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if not body:
            raise ValueError(f"小节 {unit['number']} 的正文为空。")
        pieces.append((unit, body + "\n"))
    return pieces


def _current_task(service: Any, course_id: int, episode_number: int, *,
                  repair_missing_labels: bool = True) -> dict[str, Any]:
    status = episode_task_status(service, course_id, episode_number)
    if not status["task_ready"]:
        raise ValueError("整集任务还没生成；先运行 course episode-task。")
    current = [u["subsection_id"] for u in episode_units(
        service, course_id, episode_number, repair_missing_labels=repair_missing_labels)]
    if current != [u["subsection_id"] for u in status["units"]]:
        raise ValueError("这一集的小节已变化；重新运行 course episode-task 后再收取。")
    return status


def _source_targets(service: Any, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"subsection_id": unit["subsection_id"], "label": unit["number"] or unit["stable_key"],
             "task_dir": service.subsection_task_dir(int(unit["subsection_id"]))} for unit in units]


def check_episode_reading(service: Any, course_id: int, episode_number: int) -> dict[str, Any]:
    status = _current_task(service, course_id, episode_number, repair_missing_labels=False)
    units = status["units"]
    if not source_ledger.reading_applies(service.course(course_id)) or not any(
            source_ledger.ledger_required(service, unit["subsection_id"]) for unit in units):
        return {"reading_checked": False, "reason": "此任务没有需要此步骤的数学教材资料。", "model_calls": 0}
    record = source_ledger.load_record(Path(status["task_dir"]) / source_ledger.SOURCES_FILENAME)
    return source_ledger.check_record(service, course_id, record, _source_targets(service, units), reading_only=True)


def _episode_pieces(service: Any, course_id: int, episode_number: int) -> tuple[Path, str, list[tuple[dict[str, Any], str]]]:
    status = _current_task(service, course_id, episode_number)
    if not status["draft_exists"]:
        raise ValueError(f"还没有 OUTPUT.tex：{status['output_file']}")
    output = Path(status["output_file"])
    text = output.read_text(encoding="utf-8")
    return output, text, split_output(text, status["units"])


def _check_sources(service: Any, course_id: int, episode_number: int, output: Path,
                   pieces: list[tuple[dict[str, Any], str]]) -> dict[str, Any] | None:
    """Check the episode's source record and leave the readable report beside it."""
    if not any(source_ledger.ledger_required(service, unit["subsection_id"]) for unit, _body in pieces):
        return None
    record = source_ledger.load_record(output.parent / source_ledger.SOURCES_FILENAME)
    require_reading = source_ledger.task_requires_reading(output.parent)
    targets = _source_targets(service, [unit for unit, _ in pieces])
    for target, (_, body) in zip(targets, pieces):
        target.update(body=body, require_reading=require_reading)
    report = source_ledger.check_record(service, course_id, record, targets)
    _write_text(output.parent / source_ledger.REPORT_FILENAME,
                source_ledger.render_report(report, f"第 {int(episode_number)} 集"))
    return report


def figure_summary(service: Any, course_id: int, bodies: list[str]) -> dict[str, Any]:
    """Printed size and stray marks of every textbook figure, so Work fixes them before import."""
    from shared.scripts.online_course_diagram_backends import inspect_source_figures

    storage_dir = Path(str(service.course(int(course_id))["storage_dir"]))
    rows = [row for body in bodies for row in inspect_source_figures(body, storage_dir=storage_dir)]
    return {"count": len(rows),
            "needs_attention": [{key: row.get(key) for key in (
                "sha256", "printed_width_mm", "printed_height_mm", "scale", "stray_marks", "warnings")}
                for row in rows if row.get("warnings")]}


def check_episode_output(service: Any, course_id: int, episode_number: int) -> dict[str, Any]:
    output, _text, pieces = _episode_pieces(service, course_id, episode_number)
    checked = []
    for unit, body in pieces:
        subsection_id = int(unit["subsection_id"])
        recordings = service.subsection_recording_rows(subsection_id)
        if not recordings:
            raise ValueError(f"小节 {unit['number']} 没有关联录制段，不能按整集收取。")
        service.validate_recording_segments_latex(
            subsection_id, [str(item["session_id"]) for item in recordings], body)
        checked.append({"subsection_id": subsection_id, "number": unit["number"], "characters": len(body)})
    report = _check_sources(service, course_id, episode_number, output, pieces)
    result: dict[str, Any] = {"checked": True, "units": checked,
                              "figures": figure_summary(service, course_id, [body for _unit, body in pieces])}
    if report is not None:
        result["sources"] = {key: report[key] for key in (
            "counts", "total_items", "fuzzy_anchor_matches", "visual_checks", "warnings")}
        result["source_report"] = str(output.parent / source_ledger.REPORT_FILENAME)
    return result


def collect_episode_output(service: Any, course_id: int, episode_number: int,
                           emit: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Validate every body first; then import each without recompiling; compile once."""
    emit = emit or (lambda _message: None)
    output, text, pieces = _episode_pieces(service, course_id, episode_number)
    # Never overwrite a different draft Work left in a subsection folder.
    for unit, body in pieces:
        draft = service.subsection_task_dir(int(unit["subsection_id"])) / OUTPUT_FILENAME
        if draft.is_file() and draft.read_text(encoding="utf-8") not in ("", body):
            raise ValueError(f"小节 {unit['number']} 的任务文件夹里已有另一份 OUTPUT.tex：{draft}。先处理它再收取整集。")
    checked = check_episode_output(service, course_id, episode_number)
    # Each subsection's import checks its own part of the record, so split it first.
    ledger_path = output.parent / source_ledger.SOURCES_FILENAME
    unit_records: dict[int, str] = {}
    if "sources" in checked:
        whole = source_ledger.load_record(ledger_path)
        for entry in whole["units"]:
            subsection_id = int(entry["subsection_id"])
            unit_record = json.dumps({"schema_version": whole.get("schema_version"), "units": [entry]},
                                     ensure_ascii=False, indent=2) + "\n"
            existing = service.subsection_task_dir(subsection_id) / source_ledger.SOURCES_FILENAME
            if existing.is_file() and existing.read_text(encoding="utf-8") != unit_record:
                raise ValueError(f"小节 {subsection_id} 的任务文件夹里已有另一份来源清单：{existing}。先处理它再收取整集。")
            unit_records[subsection_id] = unit_record
    emit(f"整集 {len(pieces)} 个小节全部通过本地检查，开始逐节导入（暂不编译）。")
    imported = []
    for unit, body in pieces:
        subsection_id = int(unit["subsection_id"])
        draft = service.subsection_task_dir(subsection_id) / OUTPUT_FILENAME
        _write_text(draft, body)
        if subsection_id in unit_records:
            _write_text(draft.parent / source_ledger.SOURCES_FILENAME, unit_records[subsection_id])
        try:
            result = service.import_chatgpt_work_draft(subsection_id, emit, compile_full_course=False)
        except Exception as error:
            raise RuntimeError(
                f"小节 {unit['number']} 导入失败；之前的 {len(imported)} 个小节已导入（各自有备份），"
                f"整集 OUTPUT.tex 保留原处，修正后可重新收取。原因：{error}") from error
        imported.append({"subsection_id": subsection_id, "number": unit["number"],
                         "database_backup": result.get("database_backup", ""),
                         "archived_draft": result.get("archived_draft", "")})
    emit("全部小节已导入，开始整门课程一次性重建与编译。")
    compiled = service.merge_same_subsection_latex(
        int(course_id), emit, allow_incomplete_recording_prefix=True,
        trigger="chatgpt_work_whole_episode_import")
    archived = output.parent / "imported_drafts" / f"OUTPUT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.tex"
    archived.parent.mkdir(parents=True, exist_ok=True)
    if output.read_text(encoding="utf-8") == text:
        os.replace(output, archived)
    archived_report = ""
    if "sources" in checked:
        os.replace(ledger_path, archived.with_name(archived.stem + "_SOURCES.json"))
        report_path = archived.with_name(archived.stem + "_" + source_ledger.REPORT_FILENAME)
        os.replace(output.parent / source_ledger.REPORT_FILENAME, report_path)
        archived_report = str(report_path)
    pdf_path = str(compiled.get("pdf_path") or "")
    return {"course_id": int(course_id), "episode_number": int(episode_number), "imported_units": imported,
            "full_pdf_recompiled": bool(pdf_path), "pdf_path": pdf_path,
            "pdf_sha256": str(compiled.get("pdf_sha256") or ""), "archived_draft": str(archived),
            "source_report": archived_report, "model_calls": 0}
