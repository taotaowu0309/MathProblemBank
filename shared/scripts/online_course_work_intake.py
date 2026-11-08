"""Hand a recorded episode to ChatGPT Work.

Recording keeps only the Groq transcript and the
locally detected candidate frames. ``prepare_intake_task`` lays those out for Work,
which reads the mathematics straight from those screenshots and returns its subsections
and textbook parts in ``INTAKE.json``. No frames are selected and none are copied onward.
``apply_intake`` then runs the existing window, outline, keyframe and package code with
``WorkIntakeAgent``, which answers from that file and has no way to reach a provider.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from shared.scripts.online_course_ai_agent import OnlineCourseAiAgent

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - Pillow ships with both machines' environments
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

INTAKE_DIRNAME = "episode_intake"
INTAKE_FILENAME = "INTAKE.json"
TASK_FILENAME = "TASK.md"
FRAMES_FILENAME = "FRAMES.json"
TRANSCRIPT_FILENAME = "TRANSCRIPT.md"
CATALOG_FILENAME = "REFERENCE_CATALOG.json"
EXISTING_FILENAME = "EXISTING_SUBSECTIONS.json"
SECTIONS_FILENAME = "REFERENCE_SECTIONS.json"
SHEET_COLUMNS = 3
SHEET_ROWS = 3
SHEET_CELL_WIDTH = 640
SHEET_LABEL_HEIGHT = 30
OUTLINE_ROW_KEYS = ("chapter_number", "chapter_title", "section_number", "section_title",
                    "subsection_number", "subsection_title", "end_video_time")


class WorkOnlyModeError(RuntimeError):
    """A step that only Work can answer ran outside ``course intake-apply``."""


WORK_ONLY_MESSAGE = ("这一步要交给 Work。运行 `course intake-task <course_id> <episode_number>` "
                     "取得候选帧和转写，写好 INTAKE.json 后运行 `course intake-apply`。")


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _frame_id(session_order: int, index: int) -> str:
    return f"{int(session_order)}-{int(index)}"


class WorkIntakeAgent(OnlineCourseAiAgent):
    """Answers the existing pipeline's questions from Work's file; never calls a provider."""

    REQUIRED_PROFILE_LABEL = "ChatGPT Work（INTAKE.json，无模型调用）"

    def __init__(self, intake: dict[str, Any], session_orders: dict[str, int]) -> None:  # noqa: D107
        super().__init__()
        self.intake = intake
        self.session_orders = dict(session_orders)

    @property
    def profile_label(self) -> str:
        return self.REQUIRED_PROFILE_LABEL

    def preflight(self, emit: Callable[[str], None]) -> dict[str, Any]:
        emit("这一集由 Work 决定选帧和分节，不调用模型。")
        return {"model": "none", "work_intake": True}

    def _run(self, payload: dict[str, Any], attachments: list[dict[str, Any]], system_prompt: str,
             emit: Callable[[str], None], *, stage: str) -> str:
        if stage.startswith("录制段数学正确性审核与统一生成"):
            return json.dumps(self._synthesis_answer(payload), ensure_ascii=False)
        if stage == "AI 讲义目录识别":
            return json.dumps(self._outline_answer(payload), ensure_ascii=False)
        if "参考教材" in stage and "映射" in stage:
            return json.dumps(self._mapping_answer(payload), ensure_ascii=False)
        raise WorkOnlyModeError(f"Work intake 不能回答这一步，也不会调用模型：{stage}")

    def curate_recording_window(self, course_info: dict[str, Any], window: dict[str, Any],
                                board_frames: list[dict[str, Any]], timestamped_transcript: str,
                                emit: Callable[[str], None]) -> dict[str, Any]:
        """Keep no frames: Work reads the screenshots itself, so nothing is filtered here."""
        indexes = [int(row["index"]) for row in board_frames]
        note = (f"[{_clock(float(window['start_video_time']))}] "
                f"Work reads the board in this range directly from its {len(indexes)} program screenshots.")
        return {"selected_indexes": [], "rejected_indexes": indexes,
                "mathematical_content_markdown": note, "diagram_materials": [],
                "evidence_status": "complete", "fallback_reason": ""}

    @staticmethod
    def _synthesis_answer(payload: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": OnlineCourseAiAgent.RECORDING_WINDOW_SYNTHESIS_SCHEMA_VERSION,
                "sessions": [{"session_id": str(session.get("session_id") or ""),
                              "covered_window_indexes": [int(window["window_index"])
                                                         for window in session.get("recording_windows") or []],
                              "writing_materials": [], "replacements": [], "uncertainties": [],
                              "source_errors": [], "diagram_decisions": [], "proof_obligation_resolutions": []}
                             for session in payload.get("recording_sessions") or payload.get("sessions") or []]}

    def _outline_answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        coverage_end = float(payload["coverage_end_seconds"])
        prohibited = [float(value) for value in payload.get("prohibited_boundary_times") or []]
        rows = []
        for row in self.intake["outline"]:
            end = float(row["end_video_time"])
            # A subsection may not end exactly on a recording-window edge; those edges mean nothing to Work.
            if any(abs(end - edge) <= 0.75 for edge in prohibited):
                end += 1.0
            existing = int(row.get("existing_subsection_id") or 0)
            rows.append({**{key: row[key] for key in OUTLINE_ROW_KEYS}, "end_video_time": end,
                         "assignment": "existing" if existing else "new",
                         "existing_subsection_id": existing or None,
                         "evidence": "Chosen by ChatGPT Work after reading the transcript and frames."})
        rows[-1]["end_video_time"] = coverage_end
        return {"segments": rows}

    def _mapping_row(self, target: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
        wanted: set[int] = set()
        title = str(target.get("title") or "").strip().casefold()
        for row in self.intake["outline"]:
            if str(row["subsection_title"]).strip().casefold() == title:
                wanted = {int(value) for value in row.get("reference_part_ids") or []}
        chosen = [int(part["part_id"]) for part in catalog if int(part["part_id"]) in wanted]
        return {"selected_parts": [{"part_id": part_id, "reason": "Chosen by ChatGPT Work.", "coverage": []}
                                   for part_id in chosen],
                "overall_reason": "Chosen by ChatGPT Work from the textbook catalog.",
                "uncovered_target_topics": []}

    def _mapping_answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog = list(payload.get("reference_catalog") or [])
        if "target_subsections" in payload:
            return {"subsections": [{"subsection_id": int(target["subsection_id"]), **self._mapping_row(target, catalog)}
                                    for target in payload["target_subsections"]]}
        return self._mapping_row(dict(payload["target_subsection"]), catalog)


def intake_dir(service: Any, course_id: int, episode_number: int) -> Path:
    course = service.course(int(course_id))
    return Path(str(course["storage_dir"])) / INTAKE_DIRNAME / f"episode_{int(episode_number):03d}"


def _episode_row(service: Any, course_id: int, episode_number: int) -> Any:
    for row in service.episodes(int(course_id)):
        if int(row["episode_number"]) == int(episode_number):
            return row
    raise ValueError(f"课程 {course_id} 没有第 {episode_number} 集。")


def locked_session_ids(storage_dir: Path, episode_number: int) -> set[str]:
    """Recording sessions an earlier package already locked; their evidence is never redone."""
    manifest = (Path(storage_dir) / "episodes" / f"episode_{int(episode_number):03d}"
                / "chatgpt_package" / "package_manifest.json")
    try:
        board = json.loads(manifest.read_text(encoding="utf-8"))["incremental_processing"][
            "stage_processed_session_ids"]["board"]
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    return {str(value) for value in board if str(value)}


def _session_frames(service: Any, episode_id: int) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """The sessions still to be decided: a later recording of a packaged episode, or all of them."""
    material = service._canonical_episode_material(service._episode_material(int(episode_id)))
    episode = material["episode"]
    locked = locked_session_ids(Path(str(service.course(int(episode["course_id"]))["storage_dir"])),
                                int(episode["episode_number"]))
    sessions = [session for session in service._recording_session_ranges(material)
                if str(session["session_id"]) not in locked]
    frames: list[dict[str, Any]] = []
    directories = {str(row["id"]): Path(str(row["session_dir"])) for row in material.get("sessions") or []}
    for session in sessions:
        session_id = str(session["session_id"])
        order = int(session["session_order"])
        candidates = service._read_stream_manifest(directories[session_id]).get("candidates") or []
        for index, candidate in enumerate(candidates, start=1):
            path = Path(str(candidate.get("path") or ""))
            if isinstance(candidate, dict) and path.is_file():
                seconds = float(candidate.get("video_time") or 0.0)
                frames.append({"id": _frame_id(order, index), "session_id": session_id, "index": index,
                               "video_time": seconds, "time": _clock(seconds),
                               "path": str(path), "change_type": str(candidate.get("change_type") or "")})
    return material, sessions, frames


def _font(size: int) -> Any:
    for name in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _write_contact_sheets(frames: list[dict[str, Any]], folder: Path) -> None:
    """Number the candidates on 3x3 sheets, which is how Work sees a whole lecture's board."""
    if Image is None:
        raise RuntimeError("生成候选帧缩略图需要 Pillow。")
    folder.mkdir(parents=True, exist_ok=True)
    font = _font(20)
    per_sheet = SHEET_COLUMNS * SHEET_ROWS
    for sheet_number, start in enumerate(range(0, len(frames), per_sheet), start=1):
        group = frames[start:start + per_sheet]
        cells: list[Any] = []
        for frame in group:
            with Image.open(frame["path"]) as opened:
                image = opened.convert("RGB")
            ratio = SHEET_CELL_WIDTH / max(1, image.width)
            cells.append(image.resize((SHEET_CELL_WIDTH, max(1, int(image.height * ratio)))))
        cell_height = max(cell.height for cell in cells) + SHEET_LABEL_HEIGHT
        rows = (len(group) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
        sheet = Image.new("RGB", (SHEET_COLUMNS * SHEET_CELL_WIDTH, rows * cell_height), "white")
        draw = ImageDraw.Draw(sheet)
        for position, (frame, cell) in enumerate(zip(group, cells)):
            x = position % SHEET_COLUMNS * SHEET_CELL_WIDTH
            y = position // SHEET_COLUMNS * cell_height
            draw.text((x + 6, y + 4), f"{frame['id']}   {frame['time']}", fill="black", font=font)
            sheet.paste(cell, (x, y + SHEET_LABEL_HEIGHT))
            frame["sheet"] = f"sheets/sheet_{sheet_number:03d}.jpg"
        sheet.save(folder / f"sheet_{sheet_number:03d}.jpg", quality=80)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _existing_subsections(service: Any, course_id: int) -> list[dict[str, Any]]:
    return [{key: row.get(key) for key in ("subsection_id", "chapter_number", "chapter_title", "section_number",
                                           "section_title", "subsection_number", "subsection_title")}
            for row in (dict(item) for item in service.course_subsections(int(course_id)))
            if int(row.get("subsection_id") or 0) > 0]


def _saved_segments(service: Any, course_id: int, episode_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in service.outline_segments(int(course_id)) if int(row["episode_id"]) == int(episode_id)]


def _task_text(course: Any, episode: Any, lecture_seconds: float, frames: list[dict[str, Any]],
               sheets: int, math_limit: int | None, sections: bool = False) -> str:
    limit = ([f"- At most {math_limit} subsections for this episode: this is a textbook, so at most one per ten",
              "  minutes of lecture (under ten minutes counts as one). A subsection may cross a textbook section",
              "  or chapter. Fewer is fine; do not split a single argument."] if math_limit else [])
    return "\n".join([
        "CHATGPT WORK EPISODE INTAKE",
        "",
        f"Course {course['course_code']} {course['title']}, episode {episode['episode_number']}: {episode['title']}.",
        f"Lecture length {lecture_seconds / 60:.1f} minutes, {len(frames)} candidate frames on {sheets} sheets.",
        "No model call has been made for this episode, and none is needed: you make these decisions.",
        "",
        "READ",
        f"1. `{TRANSCRIPT_FILENAME}` in full. It is Groq speech recognition with errors; read it for sense.",
        f"2. Every image in `sheets/`. Each cell is labelled with its frame id and time. To read a cell that is",
        f"   too small, open the original at its `path` in `{FRAMES_FILENAME}`. A transcript alone misses the board:",
        "   do not skip the sheets. The mathematics on the board comes straight from these screenshots; there is",
        "   no separate record of it, and you do not select frames.",
        f"3. `{CATALOG_FILENAME}`, the course's textbook sections, and `{EXISTING_FILENAME}`, its subsections so far.",
        "",
        "DECIDE",
        "- Subsections: follow the topics the teacher actually covers, numbered to continue the existing outline.",
        *limit,
        *([f"- This course's units are the textbook's own sections: copy each row's numbers and titles exactly from",
           f"  `{SECTIONS_FILENAME}`."] if sections else []),
        "- Textbooks: for each subsection, the catalog part ids you will need, primary textbook first. An empty",
        "  list is allowed when no section applies.",
        "",
        f"WRITE `{INTAKE_FILENAME}` in this folder, then run `course intake-apply {course['id']} "
        f"{episode['episode_number']}`:",
        "",
        "```json",
        json.dumps({"outline": [{"chapter_number": 1, "chapter_title": "...", "section_number": 2,
                                 "section_title": "...", "subsection_number": 1, "subsection_title": "...",
                                 "end_video_time": round(lecture_seconds, 1), "reference_part_ids": [],
                                 "existing_subsection_id": None}]}, ensure_ascii=False, indent=2),
        "```",
        "",
        "`end_video_time` is in seconds, in increasing order; the last row runs to the end of the lecture.",
        "Set `existing_subsection_id` only when a row continues a subsection in the existing list.",
        "Titles are in English. Then continue with `course episode-task`: you write the lecture notes in one pass.",
        "",
    ])


def prepare_intake_task(service: Any, course_id: int, episode_number: int,
                        emit: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Lay the episode's evidence out for Work. Local only: no model is reachable from here."""
    emit = emit or (lambda _message: None)
    course = service.course(int(course_id))
    episode = _episode_row(service, course_id, episode_number)
    episode_id = int(episode["id"])
    service.wait_for_incremental_episode(episode_id, emit)
    service.retry_failed_incremental_chunks(episode_id, emit)
    material, sessions, frames = _session_frames(service, episode_id)
    if not sessions:
        raise ValueError("这一集没有待整理的录制批次（没有录制，或材料已经全部锁定）。")
    transcript = "\n\n".join(
        service._session_transcript_time_range(material, session, float(session["owned_start_video_time"]),
                                               float(session["owned_end_video_time"]) + 1.0)
        for session in sessions).strip()
    if not transcript:
        raise ValueError("这一集还没有转写；先确认 Groq 转写完成。")
    lecture_end = max(float(session["owned_end_video_time"]) for session in sessions)
    lecture_start = min(float(session["owned_start_video_time"]) for session in sessions)
    target = intake_dir(service, course_id, episode_number)
    target.mkdir(parents=True, exist_ok=True)
    if (target / "sheets").exists():
        shutil.rmtree(target / "sheets")
    _write_contact_sheets(frames, target / "sheets")
    catalog, _fingerprint = service._reference_mapping_catalog(int(course_id))
    (target / TRANSCRIPT_FILENAME).write_text(transcript + "\n", encoding="utf-8")
    _write_json(target / FRAMES_FILENAME, frames)
    _write_json(target / CATALOG_FILENAME, catalog)
    _write_json(target / EXISTING_FILENAME, _existing_subsections(service, course_id))
    sections = service._uses_reference_section_outline(course)
    if sections:
        _write_json(target / SECTIONS_FILENAME, service.reference_section_catalog(int(course_id)))
    sheets = len(list((target / "sheets").glob("*.jpg")))
    limit = service._math_outline_subsection_limit(course, lecture_end, _saved_segments(service, course_id, episode_id))
    (target / TASK_FILENAME).write_text(
        _task_text(course, episode, lecture_end - lecture_start, frames, sheets, limit, sections), encoding="utf-8")
    return {"task_file": str(target / TASK_FILENAME), "intake_file": str(target / INTAKE_FILENAME),
            "candidate_frames": len(frames), "contact_sheets": sheets,
            "lecture_seconds": round(lecture_end - lecture_start, 3),
            "maximum_subsections": limit, "model_calls": 0}


def validate_intake(raw: Any, part_ids: set[int], existing_ids: set[int],
                    lecture_start: float, lecture_end: float, limit: int | None) -> dict[str, Any]:
    """Refuse a bad file before anything is locked, naming each problem."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ValueError(f"{INTAKE_FILENAME} 必须是 JSON 对象。")
    outline = raw.get("outline")
    if not isinstance(outline, list) or not outline:
        problems.append("outline 不能为空")
        outline = []
    previous_end = lecture_start
    titles: list[str] = []
    for position, row in enumerate(outline, start=1):
        if not isinstance(row, dict) or any(key not in row for key in OUTLINE_ROW_KEYS):
            problems.append(f"outline 第 {position} 行缺字段（需要 {'、'.join(OUTLINE_ROW_KEYS)}）")
            continue
        try:
            end = float(row["end_video_time"])
            numbers = [int(row[key]) for key in ("chapter_number", "section_number", "subsection_number")]
        except (TypeError, ValueError):
            problems.append(f"outline 第 {position} 行的编号或 end_video_time 不是数字")
            continue
        if min(numbers) < 0 or end <= previous_end:
            problems.append(f"outline 第 {position} 行的编号为负或 end_video_time 没有递增")
        previous_end = end
        title = str(row["subsection_title"]).strip()
        titles.append(title.casefold())
        if not title or re.search(r"[㐀-鿿]", title):
            problems.append(f"outline 第 {position} 行的 subsection_title 必须是英文且不能为空")
        unknown_parts = {int(value) for value in row.get("reference_part_ids") or []} - part_ids
        if unknown_parts:
            problems.append(f"outline 第 {position} 行的 reference_part_ids 不在教材目录里：{sorted(unknown_parts)}")
        existing = int(row.get("existing_subsection_id") or 0)
        if existing and existing not in existing_ids:
            problems.append(f"outline 第 {position} 行的 existing_subsection_id 不存在：{existing}")
    if outline and abs(previous_end - lecture_end) > 60.0:
        problems.append(f"outline 没有写到讲课结束（最后 {previous_end:.0f} 秒，结束于 {lecture_end:.0f} 秒）")
    if limit is not None and len(set(titles)) > limit:
        problems.append(f"分了 {len(set(titles))} 节，这一集最多 {limit} 节（每 10 分钟最多 1 节）")
    if problems:
        raise ValueError(f"{INTAKE_FILENAME} 未通过检查，没有写入任何东西：\n- " + "\n- ".join(problems))
    return {"outline": outline}


def apply_intake(service: Any, course_id: int, episode_number: int,
                 emit: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the existing locking and packaging code with Work's decisions as its answers."""
    emit = emit or (lambda _message: None)
    course = service.course(int(course_id))
    episode = _episode_row(service, course_id, episode_number)
    episode_id = int(episode["id"])
    path = intake_dir(service, course_id, episode_number) / INTAKE_FILENAME
    if not path.is_file():
        raise ValueError(f"还没有 {path}；先运行 course intake-task 并按其中 TASK.md 写好。")
    material, sessions, _frames = _session_frames(service, episode_id)
    if not sessions:
        raise ValueError("这一集没有待整理的录制批次（没有录制，或材料已经全部锁定）。")
    lecture_end = max(float(session["owned_end_video_time"]) for session in sessions)
    lecture_start = min(float(session["owned_start_video_time"]) for session in sessions)
    catalog, _fingerprint = service._reference_mapping_catalog(int(course_id))
    intake = validate_intake(
        json.loads(path.read_text(encoding="utf-8-sig")),
        {int(part["part_id"]) for part in catalog},
        {int(row["subsection_id"]) for row in _existing_subsections(service, course_id)},
        lecture_start, lecture_end,
        service._math_outline_subsection_limit(course, lecture_end, _saved_segments(service, course_id, episode_id)))
    orders = {str(session["session_id"]): int(session["session_order"]) for session in sessions}
    directories = {str(session["session_id"]): Path(str(next(
        row["session_dir"] for row in material.get("sessions") or [] if str(row["id"]) == str(session["session_id"]))))
        for session in sessions}
    agent = WorkIntakeAgent(intake, orders)
    service.activate_work_intake(int(course_id), agent)
    try:
        # Decisions from a failed earlier attempt must not stick: nothing is locked until the package is ready.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for session_id, directory in directories.items():
            windows = service._recording_agent_windows_path(directory)
            if windows.is_file():
                os.replace(windows, windows.with_name(f"{windows.stem}_before_intake_{stamp}.json"))
            service._schedule_recording_agent_windows(session_id, finalize=True)
        service.wait_for_recording_agent_windows(episode_id, emit)
        failed = [session_id for session_id, directory in directories.items()
                  if service._has_failed_recording_agent_window(directory)]
        if failed:
            raise RuntimeError("这些录制批次的窗口没有锁定：" + "、".join(failed))
        result = service.prepare_episode_chatgpt_package(episode_id, emit)
    finally:
        service.deactivate_work_intake(int(course_id))
    ready = service._ready_episode_package_result(episode_id)
    if ready is None:
        with service.connect(rows=True) as connection:
            row = connection.execute("SELECT package_error FROM course_episodes WHERE id=?", (episode_id,)).fetchone()
        raise RuntimeError("材料包没有生成：" + str(row["package_error"] if row else "未知原因"))
    saved = _saved_segments(service, course_id, episode_id)
    return {"episode_id": episode_id, "package_path": str(result.get("path") or result.get("package_path") or ""),
            "saved_outline_segments": len(saved),
            "subsections": [str(row.get("subsection_title") or "") for row in saved], "model_calls": 0}


__all__ = ["locked_session_ids", "WorkIntakeAgent", "WorkOnlyModeError", "WORK_ONLY_MESSAGE", "apply_intake", "intake_dir",
           "prepare_intake_task", "validate_intake"]
