from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS, ApplicationPaths


MAX_LEARNER_PROFILE_BYTES = 256 * 1024
SUPPORTED_PROFILE_SUFFIXES = {".md", ".txt"}


def _discipline_name(discipline: str) -> str:
    return "physics" if str(discipline).casefold() == "physics" else "math"


def learner_profile_path(
    discipline: str = "math",
    *,
    app_paths: ApplicationPaths = APP_PATHS,
) -> Path:
    name = _discipline_name(discipline)
    return app_paths.settings_dir / f"ai_{name}_learner_profile.txt"


def legacy_learner_profile_path(
    discipline: str = "math",
    *,
    app_paths: ApplicationPaths = APP_PATHS,
) -> Path:
    name = _discipline_name(discipline)
    return app_paths.application_root / "shared" / "templates" / f"ai_{name}_learner_profile.txt"


def _read_profile(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size > MAX_LEARNER_PROFILE_BYTES:
        raise ValueError(f"学习画像超过 {MAX_LEARNER_PROFILE_BYTES // 1024} KiB 限制。")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("学习画像必须是 UTF-8 文本。") from error
    if "\x00" in text:
        raise ValueError("学习画像不能包含 NUL 字符。")
    return text.strip()


def load_learner_profile(
    discipline: str = "math",
    *,
    app_paths: ApplicationPaths = APP_PATHS,
) -> str:
    user_path = learner_profile_path(discipline, app_paths=app_paths)
    if user_path.is_file():
        return _read_profile(user_path)
    if app_paths.public_release:
        return ""
    return _read_profile(legacy_learner_profile_path(discipline, app_paths=app_paths))


def learner_profile_status(
    discipline: str = "math",
    *,
    app_paths: ApplicationPaths = APP_PATHS,
) -> dict[str, Any]:
    user_path = learner_profile_path(discipline, app_paths=app_paths)
    legacy_path = legacy_learner_profile_path(discipline, app_paths=app_paths)
    if user_path.is_file():
        content = _read_profile(user_path)
        source = "user" if content else "empty"
        active_path: Path | None = user_path
    elif not app_paths.public_release and legacy_path.is_file():
        content = _read_profile(legacy_path)
        source = "legacy"
        active_path = legacy_path
    else:
        content = ""
        source = "none"
        active_path = None
    return {
        "source": source,
        "configured": bool(content),
        "content": content,
        "user_path": user_path,
        "active_path": active_path,
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def import_learner_profile(
    source: Path,
    discipline: str = "math",
    *,
    app_paths: ApplicationPaths = APP_PATHS,
) -> dict[str, Any]:
    source = Path(source).expanduser().resolve()
    if source.suffix.casefold() not in SUPPORTED_PROFILE_SUFFIXES:
        raise ValueError("学习画像只支持 UTF-8 编码的 .txt 或 .md 文件。")
    if not source.is_file():
        raise FileNotFoundError(f"学习画像文件不存在：{source}")
    content = _read_profile(source)
    destination = learner_profile_path(discipline, app_paths=app_paths)
    serialized = content + "\n" if content else ""
    _atomic_write(destination, serialized)
    if _read_profile(destination) != content:
        raise OSError("学习画像写入后回读校验失败。")
    return learner_profile_status(discipline, app_paths=app_paths)


def clear_learner_profile(
    discipline: str = "math",
    *,
    app_paths: ApplicationPaths = APP_PATHS,
) -> dict[str, Any]:
    destination = learner_profile_path(discipline, app_paths=app_paths)
    _atomic_write(destination, "")
    if destination.read_bytes() != b"":
        raise OSError("学习画像清空后回读校验失败。")
    return learner_profile_status(discipline, app_paths=app_paths)
