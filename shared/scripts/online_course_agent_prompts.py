from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

DEFAULT_ONLINE_COURSE_STORAGE_ROOT = APP_PATHS.online_course_root
COURSE_AGENT_PROMPTS_DIRNAME = "agent_prompts"
COURSE_AGENT_PROMPT_METADATA = "_course.json"
COURSE_AGENT_PROMPT_SUFFIXES = {".md", ".txt"}
MAX_COURSE_AGENT_PROMPT_FILE_BYTES = 128 * 1024
MAX_COURSE_AGENT_PROMPT_TOTAL_BYTES = 512 * 1024


def _safe_course_code(course_code: Any) -> str:
    value = str(course_code or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(f"无效的网课代码，无法定位常驻提示词目录：{value!r}")
    return value


def course_agent_prompts_root(
    storage_root: Path = DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
) -> Path:
    return Path(storage_root) / COURSE_AGENT_PROMPTS_DIRNAME


def course_agent_prompt_directory(
    course_code: Any,
    storage_root: Path = DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
) -> Path:
    return course_agent_prompts_root(storage_root) / _safe_course_code(course_code)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def ensure_course_agent_prompt_directory(
    *,
    course_id: int,
    course_code: Any,
    course_title: Any,
    storage_root: Path = DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
) -> Path:
    directory = course_agent_prompt_directory(course_code, storage_root)
    directory.mkdir(parents=True, exist_ok=True)
    metadata_path = directory / COURSE_AGENT_PROMPT_METADATA
    metadata = {
        "course_id": int(course_id),
        "course_code": _safe_course_code(course_code),
        "course_title": str(course_title or "").strip(),
        "purpose": (
            "Store arbitrary persistent user instructions for this online course. "
            "Every .md and .txt file in this directory is injected into each "
            "substantive online-course Agent request."
        ),
    }
    try:
        current = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = None
    if current != metadata:
        _atomic_json(metadata_path, metadata)
    return directory


def _metadata_matches(
    metadata: dict[str, Any],
    *,
    course_id: int,
    course_title: str,
) -> bool:
    if course_id and int(metadata.get("course_id") or 0) == course_id:
        return True
    return bool(
        course_title
        and str(metadata.get("course_title") or "").strip() == course_title
    )


def _resolve_course_prompt_directory(
    *,
    course_id: int = 0,
    course_code: Any = "",
    course_title: Any = "",
    storage_root: Path = DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
) -> Path | None:
    root = course_agent_prompts_root(storage_root)
    code = str(course_code or "").strip()
    if code:
        directory = course_agent_prompt_directory(code, storage_root)
        return directory if directory.is_dir() else None
    title = str(course_title or "").strip()
    if not root.is_dir() or (not course_id and not title):
        return None
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            metadata = json.loads(
                (directory / COURSE_AGENT_PROMPT_METADATA).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict) and _metadata_matches(
            metadata,
            course_id=int(course_id or 0),
            course_title=title,
        ):
            return directory
    return None


def load_course_agent_instructions(
    *,
    course_id: int = 0,
    course_code: Any = "",
    course_title: Any = "",
    storage_root: Path = DEFAULT_ONLINE_COURSE_STORAGE_ROOT,
) -> dict[str, Any]:
    directory = _resolve_course_prompt_directory(
        course_id=int(course_id or 0),
        course_code=course_code,
        course_title=course_title,
        storage_root=storage_root,
    )
    if directory is None:
        return {"directory": "", "files": [], "text": ""}
    prompt_files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.name.startswith("_")
        and path.suffix.casefold() in COURSE_AGENT_PROMPT_SUFFIXES
    )
    total_bytes = 0
    sections: list[str] = []
    names: list[str] = []
    for path in prompt_files:
        size = path.stat().st_size
        if size > MAX_COURSE_AGENT_PROMPT_FILE_BYTES:
            raise RuntimeError(
                f"网课常驻提示词文件过大：{path}（上限 "
                f"{MAX_COURSE_AGENT_PROMPT_FILE_BYTES} 字节）。"
            )
        total_bytes += size
        if total_bytes > MAX_COURSE_AGENT_PROMPT_TOTAL_BYTES:
            raise RuntimeError(
                "当前网课常驻提示词总量超过上限："
                f"{MAX_COURSE_AGENT_PROMPT_TOTAL_BYTES} 字节。"
            )
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        names.append(path.name)
        sections.append(f"<course_instruction_file name={json.dumps(path.name, ensure_ascii=False)}>\n{content}\n</course_instruction_file>")
    if not sections:
        return {"directory": str(directory), "files": [], "text": ""}
    text = (
        "<persistent_online_course_instructions>\n"
        "These are user-authored, course-specific persistent instructions. "
        "They are mandatory for this course and are supplied on every substantive "
        "online-course Agent request. Apply each instruction within its stated scope; "
        "do not silently omit, replace, or reinterpret it as optional guidance.\n\n"
        + "\n\n".join(sections)
        + "\n</persistent_online_course_instructions>"
    )
    return {"directory": str(directory), "files": names, "text": text}
