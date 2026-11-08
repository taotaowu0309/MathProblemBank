from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
SETTINGS_PATH = APP_PATHS.settings_dir / "ai_math_reference_library.json"
DEFAULT_REFERENCE_ROOT = APP_PATHS.reference_library_root
SUPPORTED_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".tex",
    ".pdf",
    ".docx",
    ".html",
    ".htm",
}
SKIPPED_DIRECTORIES = {
    ".git",
    ".svn",
    ".hg",
    ".obsidian",
    ".trash",
    "__pycache__",
    "node_modules",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(slots=True)
class ReferenceLibraryRoot:
    id: str
    path: str
    name: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReferenceLibraryRoot | None":
        path = Path(str(raw.get("path") or "")).expanduser()
        if not path.is_absolute():
            return None
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex),
            path=str(path.resolve()),
            name=str(raw.get("name") or path.name or path),
            enabled=bool(raw.get("enabled", True)),
        )


class ReferenceLibraryStore:
    """Local, user-managed roots used only as read-only mathematical references."""

    def __init__(self, path: Path = SETTINGS_PATH, *, auto_add_default: bool = True) -> None:
        self.path = Path(path)
        self.auto_add_default = bool(auto_add_default)
        self.roots: list[ReferenceLibraryRoot] = []
        self._load()

    def _load(self) -> None:
        raw: dict[str, Any] = {}
        if self.path.is_file():
            try:
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                raw = parsed if isinstance(parsed, dict) else {}
            except (OSError, json.JSONDecodeError):
                raw = {}
        for item in raw.get("roots") or []:
            root = ReferenceLibraryRoot.from_dict(item) if isinstance(item, dict) else None
            if root is not None:
                self.roots.append(root)
        if not self.roots and self.auto_add_default and DEFAULT_REFERENCE_ROOT.is_dir():
            self.roots.append(
                ReferenceLibraryRoot(
                    id=uuid.uuid4().hex,
                    path=str(DEFAULT_REFERENCE_ROOT.resolve()),
                    name="数学共享笔记",
                    enabled=True,
                )
            )
            self.save()

    def save(self) -> None:
        _atomic_json(
            self.path,
            {"version": 1, "roots": [asdict(root) for root in self.roots]},
        )

    def add(self, path: str | Path, name: str = "") -> ReferenceLibraryRoot:
        target = Path(path).expanduser()
        if not target.is_absolute() or not target.is_dir():
            raise ValueError("数学资料库目录不存在或不是绝对路径。")
        resolved = str(target.resolve())
        existing = next(
            (root for root in self.roots if root.path.casefold() == resolved.casefold()),
            None,
        )
        if existing is not None:
            existing.enabled = True
            if name.strip():
                existing.name = name.strip()
            self.save()
            return existing
        root = ReferenceLibraryRoot(
            id=uuid.uuid4().hex,
            path=resolved,
            name=name.strip() or target.name or resolved,
            enabled=True,
        )
        self.roots.append(root)
        self.save()
        return root

    def remove(self, root_id: str) -> bool:
        before = len(self.roots)
        self.roots = [root for root in self.roots if root.id != str(root_id)]
        if len(self.roots) != before:
            self.save()
            return True
        return False

    def set_enabled(self, root_id: str, enabled: bool) -> bool:
        for root in self.roots:
            if root.id == str(root_id):
                root.enabled = bool(enabled)
                self.save()
                return True
        return False

    def enabled_roots(self) -> list[ReferenceLibraryRoot]:
        return [root for root in self.roots if root.enabled and Path(root.path).is_dir()]

    def iter_files(self) -> Iterable[tuple[ReferenceLibraryRoot, Path]]:
        seen: set[str] = set()
        for root in self.enabled_roots():
            directory = Path(root.path)
            for current, dirnames, filenames in os.walk(directory):
                dirnames[:] = [
                    name
                    for name in dirnames
                    if name.casefold() not in SKIPPED_DIRECTORIES and not name.startswith(".")
                ]
                for filename in filenames:
                    path = Path(current) / filename
                    if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
                        continue
                    key = str(path.resolve()).casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    yield root, path

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        total = 0
        total_bytes = 0
        for _root, path in self.iter_files():
            suffix = path.suffix.casefold()
            counts[suffix] = counts.get(suffix, 0) + 1
            total += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        return {
            "enabled_root_count": len(self.enabled_roots()),
            "file_count": total,
            "total_bytes": total_bytes,
            "suffix_counts": counts,
        }
