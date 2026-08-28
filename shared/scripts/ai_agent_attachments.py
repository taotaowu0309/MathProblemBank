from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
ATTACHMENT_ROOT = APP_PATHS.cache_dir / "ai_agent_attachments"
MAX_ATTACHMENT_COUNT = 20
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 150 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _safe_name(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "attachment")).strip(" .")
    return value[:120] or "attachment"


def _mime_for(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return "image/jpeg" if mime == "image/jpg" else mime


def normalize_attachment(raw: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(str(raw.get("path") or "")).expanduser()
    if not path.is_file():
        return None
    try:
        size = int(path.stat().st_size)
    except OSError:
        return None
    mime = str(raw.get("mime_type") or _mime_for(path))
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex),
        "name": _safe_name(str(raw.get("name") or path.name)),
        "path": str(path.resolve()),
        "mime_type": mime,
        "kind": "image" if mime in IMAGE_MIME_TYPES else "file",
        "size": size,
    }


def store_files(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    sources = [Path(path).expanduser().resolve() for path in paths]
    if len(sources) > MAX_ATTACHMENT_COUNT:
        raise ValueError(f"一次最多添加 {MAX_ATTACHMENT_COUNT} 个附件。")
    total = 0
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"附件不存在：{source}")
        size = int(source.stat().st_size)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件 {source.name} 超过 50 MB。")
        total += size
    if total > MAX_BATCH_BYTES:
        raise ValueError("本批附件总大小超过 150 MB。")

    ATTACHMENT_ROOT.mkdir(parents=True, exist_ok=True)
    stored: list[dict[str, Any]] = []
    for source in sources:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        destination = ATTACHMENT_ROOT / f"{uuid.uuid4().hex[:10]}_{digest}_{_safe_name(source.name)}"
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        item = normalize_attachment({"path": str(destination), "name": source.name})
        if item is not None:
            stored.append(item)
    return stored


def store_image_bytes(data: bytes, *, name: str = "粘贴的图片.png", mime_type: str = "image/png") -> dict[str, Any]:
    if not data:
        raise ValueError("剪贴板图片为空。")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("剪贴板图片超过 50 MB。")
    suffix = mimetypes.guess_extension(mime_type) or ".png"
    ATTACHMENT_ROOT.mkdir(parents=True, exist_ok=True)
    destination = ATTACHMENT_ROOT / f"{uuid.uuid4().hex}_{_safe_name(Path(name).stem)}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)
    item = normalize_attachment({"path": str(destination), "name": name, "mime_type": mime_type})
    if item is None:
        raise OSError("无法保存剪贴板图片。")
    return item


def attachment_manifest(attachments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for raw in attachments:
        item = normalize_attachment(raw)
        if item is None:
            continue
        manifest.append(
            {
                "name": item["name"],
                "path": item["path"],
                "mime_type": item["mime_type"],
                "kind": item["kind"],
                "size": item["size"],
            }
        )
    return manifest
