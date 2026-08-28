from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


TEXT_SUFFIXES = {
    ".bib", ".cfg", ".cls", ".css", ".csv", ".html", ".ini", ".js", ".json",
    ".lean", ".md", ".ps1", ".py", ".pyw", ".qml", ".sql", ".sty", ".tex",
    ".toml", ".ts", ".tsx", ".txt", ".vbs", ".xml", ".yaml", ".yml",
}
CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".lean", ".php",
    ".ps1", ".py", ".pyw", ".rb", ".rs", ".sh", ".ts", ".tsx", ".vbs",
}
SKIPPED_DIRECTORIES = {
    ".git", ".idea", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
    "__pycache__", "node_modules",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")


class WorkspaceToolManager:
    """Repository-scoped inspection, editing and verification tools for the AI assistant."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve()
        if not self.root_dir.is_dir():
            raise ValueError(f"工作区不存在：{self.root_dir}")
        self.cache_dir = self.root_dir / "shared" / "ui" / "cache" / "ai_agent_workspace"

    def _resolve(self, value: str | Path = "", *, allow_missing: bool = False) -> Path:
        raw = Path(str(value or "."))
        target = (raw if raw.is_absolute() else self.root_dir / raw).resolve()
        if target != self.root_dir and self.root_dir not in target.parents:
            raise ValueError("仓库工具只能访问当前 MathProblemBank 工作区内的路径。")
        if not allow_missing and not target.exists():
            raise ValueError(f"路径不存在：{target}")
        return target

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root_dir).as_posix()

    @staticmethod
    def _read_text(path: Path, max_chars: int = 200000) -> tuple[str, bool]:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"文本文件过大，拒绝整文件读取：{path}")
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ValueError(f"文件看起来是二进制文件：{path}")
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        limit = max(1000, min(int(max_chars), 500000))
        return text[:limit], len(text) > limit

    def list_tree(self, path: str = "", depth: int = 3, limit: int = 800) -> dict[str, Any]:
        base = self._resolve(path)
        if not base.is_dir():
            raise ValueError("目标不是目录。")
        depth = max(1, min(int(depth), 8))
        limit = max(1, min(int(limit), 3000))
        entries: list[dict[str, Any]] = []
        stack: list[tuple[Path, int]] = [(base, 0)]
        while stack and len(entries) < limit:
            current, level = stack.pop()
            try:
                children = sorted(current.iterdir(), key=lambda item: (item.is_file(), item.name.casefold()))
            except OSError:
                continue
            directories: list[Path] = []
            for child in children:
                if child.name in SKIPPED_DIRECTORIES:
                    continue
                try:
                    is_dir = child.is_dir()
                    entries.append(
                        {
                            "path": self._relative(child),
                            "kind": "directory" if is_dir else "file",
                            "size_bytes": None if is_dir else child.stat().st_size,
                            "depth": level + 1,
                        }
                    )
                    if is_dir and level + 1 < depth:
                        directories.append(child)
                    if len(entries) >= limit:
                        break
                except OSError:
                    continue
            stack.extend((item, level + 1) for item in reversed(directories))
        return {
            "root": str(self.root_dir),
            "base_path": self._relative(base) or ".",
            "entries": entries,
            "entry_count": len(entries),
            "truncated": bool(stack) or len(entries) >= limit,
        }

    def search_text(
        self,
        query: str,
        paths: Iterable[str] | None = None,
        extensions: Iterable[str] | None = None,
        limit: int = 120,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        needle = str(query or "")
        if not needle:
            raise ValueError("搜索文字不能为空。")
        limit = max(1, min(int(limit), 500))
        suffixes = {
            (item if str(item).startswith(".") else "." + str(item)).casefold()
            for item in extensions or [] if str(item).strip()
        }
        roots = [self._resolve(item) for item in (paths or [""])]
        pattern = needle if case_sensitive else needle.casefold()
        results: list[dict[str, Any]] = []
        scanned = 0
        for root in roots:
            candidates = [root] if root.is_file() else root.rglob("*")
            for candidate in candidates:
                if len(results) >= limit:
                    break
                try:
                    if not candidate.is_file() or any(part in SKIPPED_DIRECTORIES for part in candidate.parts):
                        continue
                    if suffixes and candidate.suffix.casefold() not in suffixes:
                        continue
                    if not suffixes and candidate.suffix.casefold() not in TEXT_SUFFIXES:
                        continue
                    if candidate.stat().st_size > 4 * 1024 * 1024:
                        continue
                    text, _ = self._read_text(candidate, 4 * 1024 * 1024)
                    scanned += 1
                    for line_number, line in enumerate(text.splitlines(), 1):
                        haystack = line if case_sensitive else line.casefold()
                        column = haystack.find(pattern)
                        if column < 0:
                            continue
                        results.append(
                            {
                                "path": self._relative(candidate),
                                "line": line_number,
                                "column": column + 1,
                                "excerpt": line.strip()[:1200],
                            }
                        )
                        if len(results) >= limit:
                            break
                except (OSError, ValueError):
                    continue
            if len(results) >= limit:
                break
        return {
            "query": needle,
            "results": results,
            "result_count": len(results),
            "scanned_file_count": scanned,
            "truncated": len(results) >= limit,
        }

    def read_files(self, requests: Iterable[dict[str, Any]], max_total_chars: int = 240000) -> dict[str, Any]:
        items = [dict(item) for item in requests if isinstance(item, dict)][:20]
        if not items:
            raise ValueError("请至少提供一个要读取的仓库文件。")
        remaining = max(2000, min(int(max_total_chars), 500000))
        files: list[dict[str, Any]] = []
        for request in items:
            path = self._resolve(str(request.get("path") or ""))
            if not path.is_file():
                raise ValueError(f"目标不是文件：{path}")
            text, source_truncated = self._read_text(path, remaining)
            lines = text.splitlines()
            start = max(1, int(request.get("line_start") or 1))
            end = min(len(lines), int(request.get("line_end") or len(lines)))
            if end < start:
                raise ValueError(f"行范围无效：{path}")
            numbered = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
            numbered = numbered[:remaining]
            remaining -= len(numbered)
            files.append(
                {
                    "path": str(path),
                    "relative_path": self._relative(path),
                    "line_start": start,
                    "line_end": end,
                    "line_count": len(lines),
                    "sha256": _sha256(path),
                    "content": numbered,
                    "truncated": source_truncated or end < len(lines) or remaining <= 0,
                }
            )
            if remaining <= 0:
                break
        return {"files": files, "file_count": len(files), "remaining_char_budget": max(0, remaining)}

    def inspect_git(self, paths: Iterable[str] | None = None, include_diff: bool = True) -> dict[str, Any]:
        selected = [self._relative(self._resolve(item, allow_missing=True)) for item in paths or []]
        status_cmd = ["git", "status", "--short", "--untracked-files=all"]
        if selected:
            status_cmd.extend(["--", *selected])
        status = subprocess.run(
            status_cmd, cwd=self.root_dir, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if status.returncode != 0:
            raise RuntimeError(status.stderr.strip() or "git status 失败。")
        diff_text = ""
        if include_diff:
            command = ["git", "diff", "--no-ext-diff", "--"] + selected
            diff = subprocess.run(
                command, cwd=self.root_dir, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if diff.returncode != 0:
                raise RuntimeError(diff.stderr.strip() or "git diff 失败。")
            diff_text = diff.stdout[:120000]
        return {
            "root": str(self.root_dir),
            "status": status.stdout[:60000],
            "diff": diff_text,
            "diff_truncated": len(diff_text) >= 120000,
        }

    @staticmethod
    def _validate_read_query(query: str) -> str:
        sql = str(query or "").strip()
        if not sql:
            raise ValueError("SQL 查询不能为空。")
        stripped = re.sub(r"(?:--[^\n]*|/\*.*?\*/)", " ", sql, flags=re.S).strip()
        if ";" in stripped.rstrip(";"):
            raise ValueError("只读检查每次只允许一条 SQL。")
        if not re.match(r"^(?:select|pragma\s+(?!writable_schema)|with\b)", stripped, re.I):
            raise ValueError("只读 SQLite 工具只允许 SELECT、WITH 或安全 PRAGMA。")
        if re.search(r"\b(?:insert|update|delete|replace|drop|alter|create|attach|detach|vacuum|reindex)\b", stripped, re.I):
            raise ValueError("只读 SQLite 工具拒绝修改数据库的语句。")
        return sql.rstrip(";")

    def inspect_sqlite(self, path: str, query: str = "", limit: int = 100) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError("SQLite 路径不是文件。")
        limit = max(1, min(int(limit), 500))
        uri = target.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            schema_rows = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            result: dict[str, Any] = {
                "path": str(target),
                "relative_path": self._relative(target),
                "size_bytes": target.stat().st_size,
                "sha256": _sha256(target),
                "integrity_check": integrity,
                "schema": [dict(row) for row in schema_rows],
            }
            if query:
                sql = self._validate_read_query(query)
                cursor = conn.execute(sql)
                rows = cursor.fetchmany(limit + 1)
                result["columns"] = [item[0] for item in cursor.description or []]
                result["rows"] = [dict(row) for row in rows[:limit]]
                result["row_count"] = min(len(rows), limit)
                result["truncated"] = len(rows) > limit
            return result

    def _backup_directory(self, kind: str) -> Path:
        target = self.cache_dir / "backups" / f"{_timestamp()}_{kind}_{uuid.uuid4().hex[:8]}"
        target.mkdir(parents=True, exist_ok=False)
        return target

    @staticmethod
    def _apply_edit(original: str, edit: dict[str, Any]) -> str:
        operation = str(edit.get("operation") or "replace")
        old_text = str(edit.get("old_text") or edit.get("anchor_text") or "")
        new_text = str(edit.get("new_text") or "")
        if operation == "create_or_replace":
            return new_text
        if not old_text:
            raise ValueError(f"{operation} 操作需要 old_text。")
        occurrences = original.count(old_text)
        if occurrences != 1:
            raise ValueError(f"old_text 必须且只能匹配一次，当前匹配 {occurrences} 次。")
        if operation == "replace":
            return original.replace(old_text, new_text, 1)
        if operation == "insert_before":
            return original.replace(old_text, new_text + old_text, 1)
        if operation == "insert_after":
            return original.replace(old_text, old_text + new_text, 1)
        raise ValueError(f"不支持的补丁操作：{operation}")

    def preview_patch(self, edits: Iterable[dict[str, Any]]) -> dict[str, Any]:
        prepared = [dict(item) for item in edits if isinstance(item, dict)][:20]
        diffs: list[str] = []
        targets: list[str] = []
        recovery: list[dict[str, Any]] = []
        for edit in prepared:
            path = self._resolve(str(edit.get("path") or ""), allow_missing=True)
            original = path.read_text(encoding="utf-8") if path.is_file() else ""
            updated = self._apply_edit(original, edit)
            targets.append(str(path))
            recovery.append(
                {"path": str(path), "existed_before": path.is_file(), "original_text": original}
            )
            diffs.append("".join(difflib.unified_diff(
                original.splitlines(keepends=True), updated.splitlines(keepends=True),
                fromfile=self._relative(path) + "（修改前）", tofile=self._relative(path) + "（修改后）", n=4,
            )))
        return {"targets": targets, "diff": "\n".join(diffs)[:120000], "recovery": recovery}

    def apply_patch(self, edits: Iterable[dict[str, Any]]) -> dict[str, Any]:
        prepared = [dict(item) for item in edits if isinstance(item, dict)][:20]
        if not prepared:
            raise ValueError("补丁不能为空。")
        backup_dir = self._backup_directory("patch")
        states: list[tuple[Path, bool, bytes, str]] = []
        changed_files: list[str] = []
        before_hashes: dict[str, str] = {}
        after_hashes: dict[str, str] = {}
        diffs: list[str] = []
        try:
            targets: set[Path] = set()
            for edit in prepared:
                path = self._resolve(str(edit.get("path") or ""), allow_missing=True)
                if path in targets:
                    raise ValueError("同一个文件不能在一次补丁事务中出现多次。")
                targets.add(path)
                existed = path.is_file()
                original_bytes = path.read_bytes() if existed else b""
                original = original_bytes.decode("utf-8") if existed else ""
                expected = str(edit.get("expected_sha256") or "")
                if existed and expected and _sha256(path).casefold() != expected.casefold():
                    raise ValueError(f"文件已在读取后变化，拒绝覆盖：{path}")
                updated = self._apply_edit(original, edit)
                if updated == original and existed:
                    raise ValueError(f"补丁没有产生变化：{path}")
                states.append((path, existed, original_bytes, updated))
                relative = self._relative(path)
                if existed:
                    before_hashes[relative] = _sha256(path)
                    backup_path = backup_dir / relative
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup_path)
                diffs.append("".join(difflib.unified_diff(
                    original.splitlines(keepends=True), updated.splitlines(keepends=True),
                    fromfile=relative + "（修改前）", tofile=relative + "（修改后）", n=4,
                )))
            for path, _existed, _original, updated in states:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + f".ai_tmp_{uuid.uuid4().hex}")
                temporary.write_text(updated, encoding="utf-8", newline="")
                os.replace(temporary, path)
                changed_files.append(str(path))
                after_hashes[self._relative(path)] = _sha256(path)
            verified = all(
                path.is_file() and path.read_bytes() == updated.encode("utf-8")
                for path, _, _, updated in states
            )
            if not verified:
                raise RuntimeError("补丁写入后的逐文件回读核验失败。")
        except Exception:
            for path, existed, original, _updated in reversed(states):
                if existed:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
                elif path.exists():
                    path.unlink()
            raise
        return {
            "changed": True,
            "verified": True,
            "transaction_verified": True,
            "changed_files": changed_files,
            "changed_file_count": len(changed_files),
            "code_changed": any(Path(path).suffix.casefold() in CODE_SUFFIXES for path in changed_files),
            "backup_directory": str(backup_dir),
            "before_hashes": before_hashes,
            "after_hashes": after_hashes,
            "diff": "\n".join(diffs)[:120000],
        }

    def preview_file_operations(self, operations: Iterable[dict[str, Any]]) -> dict[str, Any]:
        prepared = [dict(item) for item in operations if isinstance(item, dict)][:20]
        targets: list[str] = []
        lines: list[str] = []
        for item in prepared:
            operation = str(item.get("operation") or "")
            source = self._resolve(str(item.get("path") or ""), allow_missing=operation in {"create_directory"})
            destination = str(item.get("destination") or "")
            targets.append(str(source))
            if destination:
                target = self._resolve(destination, allow_missing=True)
                targets.append(str(target))
                lines.append(f"{operation}: {source} -> {target}")
            else:
                lines.append(f"{operation}: {source}")
        return {"targets": targets, "diff": "\n".join(lines)}

    def manage_files(self, operations: Iterable[dict[str, Any]]) -> dict[str, Any]:
        prepared = [dict(item) for item in operations if isinstance(item, dict)][:20]
        if not prepared:
            raise ValueError("文件操作不能为空。")
        trash_dir = self._backup_directory("file_operations")
        changed: list[str] = []
        moved: list[dict[str, str]] = []
        planned: list[tuple[str, Path, Path | None]] = []
        destinations: set[Path] = set()
        for item in prepared:
            operation = str(item.get("operation") or "")
            if operation not in {"create_directory", "move", "copy", "delete_to_trash"}:
                raise ValueError(f"不支持的文件操作：{operation}")
            source = self._resolve(str(item.get("path") or ""), allow_missing=operation == "create_directory")
            if source == self.root_dir:
                raise ValueError("禁止对仓库根目录执行文件管理操作。")
            if source == self.cache_dir.resolve() or source in self.cache_dir.resolve().parents:
                raise ValueError("禁止移动或回收包含 AI 恢复数据的上层目录。")
            destination: Path | None = None
            if operation in {"move", "copy"}:
                destination = self._resolve(str(item.get("destination") or ""), allow_missing=True)
            elif operation == "delete_to_trash":
                destination = trash_dir / self._relative(source)
            if operation == "create_directory" and source.exists():
                raise ValueError(f"目录已存在：{source}")
            if destination is not None:
                if destination.exists() or destination in destinations:
                    raise ValueError(f"目标已存在或在本事务中重复：{destination}")
                destinations.add(destination)
            planned.append((operation, source, destination))
        undo: list[tuple[str, Path, Path | None]] = []
        try:
            for operation, source, destination in planned:
                if operation == "create_directory":
                    source.mkdir(parents=True, exist_ok=False)
                    undo.append(("remove", source, None))
                    changed.append(str(source))
                    continue
                assert destination is not None
                destination.parent.mkdir(parents=True, exist_ok=True)
                if operation in {"move", "delete_to_trash"}:
                    shutil.move(str(source), str(destination))
                    undo.append(("move_back", destination, source))
                elif source.is_dir():
                    shutil.copytree(source, destination)
                    undo.append(("remove", destination, None))
                else:
                    shutil.copy2(source, destination)
                    undo.append(("remove", destination, None))
                changed.extend([str(source), str(destination)])
                moved.append({"operation": operation, "source": str(source), "destination": str(destination)})
            verified = all(
                source.exists()
                if operation == "create_directory"
                else bool(destination and destination.exists() and (operation == "copy" or not source.exists()))
                for operation, source, destination in planned
            )
            if not verified:
                raise RuntimeError("文件操作后的回读核验失败。")
        except Exception:
            for action, current, original in reversed(undo):
                if action == "move_back" and current.exists() and original is not None:
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(current), str(original))
                elif action == "remove" and current.exists():
                    if current.is_dir():
                        shutil.rmtree(current)
                    else:
                        current.unlink()
            raise
        return {
            "changed": True,
            "verified": verified,
            "transaction_verified": verified,
            "changed_files": changed,
            "changed_file_count": len(changed),
            "file_operations": moved,
            "backup_directory": str(trash_dir),
        }

    @staticmethod
    def _safe_command(executable: str, arguments: Iterable[str]) -> tuple[list[str], str]:
        name = str(executable or "").casefold()
        args = [str(item) for item in arguments][:80]
        if name == "python":
            if any(item in {"-c", "-i"} for item in args):
                raise ValueError("受控 Python 命令不允许 -c 或交互模式。")
            if args[:1] == ["-m"] and len(args) > 1 and args[1].casefold() in {
                "ensurepip", "http.server", "pip", "venv",
            }:
                raise ValueError("受控 Python 命令拒绝安装依赖、启动服务器或创建运行环境。")
            return [sys.executable, *args], "python"
        if name == "git":
            if not args or args[0] not in {"status", "diff", "show", "log", "ls-files", "rev-parse"}:
                raise ValueError("受控 Git 命令只允许只读子命令。")
            return ["git", *args], "git"
        if name == "rg":
            return ["rg", *args], "rg"
        raise ValueError("只允许受控执行 python、git 或 rg。")

    def run_command(
        self,
        executable: str,
        arguments: Iterable[str],
        working_directory: str = "",
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        command, label = self._safe_command(executable, arguments)
        cwd = self._resolve(working_directory)
        if not cwd.is_dir():
            raise ValueError("命令工作目录不是目录。")
        timeout_seconds = max(5, min(int(timeout_seconds), 900))
        if label == "python":
            for argument in command[1:]:
                candidate = Path(argument)
                if candidate.suffix.casefold() not in {".py", ".pyw"}:
                    continue
                resolved = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
                if resolved != self.root_dir and self.root_dir not in resolved.parents:
                    raise ValueError("Python 只能执行当前仓库内的脚本。")
        started = time.monotonic()
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        log_dir = self.cache_dir / "command_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{_timestamp()}_{label}_{uuid.uuid4().hex[:8]}.log"
        log_path.write_text(
            "COMMAND: " + subprocess.list2cmdline(command) + "\n"
            + f"EXIT_CODE: {completed.returncode}\n\nSTDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}",
            encoding="utf-8",
        )
        return {
            "verified": completed.returncode == 0,
            "command": subprocess.list2cmdline(command),
            "working_directory": str(cwd),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[:30000],
            "stderr": completed.stderr[:30000],
            "duration_ms": duration_ms,
            "log_path": str(log_path),
        }

    def read_command_log(self, path: str, max_chars: int = 100000) -> dict[str, Any]:
        target = self._resolve(path)
        expected = (self.cache_dir / "command_logs").resolve()
        if expected not in target.parents:
            raise ValueError("只能读取 AI 助手自己的命令日志。")
        text, truncated = self._read_text(target, max_chars)
        return {"path": str(target), "content": text, "truncated": truncated}

    @staticmethod
    def _validate_migration(sql: str) -> str:
        script = str(sql or "").strip()
        if not script:
            raise ValueError("迁移 SQL 不能为空。")
        if re.search(r"\b(?:attach|detach|vacuum|pragma\s+writable_schema|load_extension)\b", script, re.I):
            raise ValueError("迁移拒绝 ATTACH、DETACH、VACUUM、writable_schema 和扩展加载。")
        return script

    def migrate_sqlite(self, path: str, sql: str) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError("SQLite 路径不是文件。")
        script = self._validate_migration(sql)
        backup_dir = self._backup_directory("sqlite")
        backup_path = backup_dir / target.name
        before_hash = _sha256(target)
        with closing(sqlite3.connect(target, timeout=10)) as source, closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)
        try:
            with closing(sqlite3.connect(target, timeout=10)) as conn:
                before_schema = {
                    (str(row[0]), str(row[1]), str(row[2] or ""))
                    for row in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
                }
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_key_violations = [list(row) for row in conn.execute("PRAGMA foreign_key_check").fetchmany(50)]
                after_schema = {
                    (str(row[0]), str(row[1]), str(row[2] or ""))
                    for row in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
                }
                conn.commit()
            if integrity.casefold() != "ok" or foreign_key_violations:
                raise RuntimeError("数据库迁移后的完整性或外键检查失败。")
        except Exception:
            shutil.copy2(backup_path, target)
            raise
        return {
            "changed": True,
            "verified": True,
            "transaction_verified": True,
            "changed_files": [str(target)],
            "changed_file_count": 1,
            "database_path": str(target),
            "backup_directory": str(backup_dir),
            "backup_path": str(backup_path),
            "before_sha256": before_hash,
            "after_sha256": _sha256(target),
            "integrity_check": integrity,
            "foreign_key_violations": foreign_key_violations,
            "schema_added": sorted(after_schema - before_schema),
            "schema_removed": sorted(before_schema - after_schema),
        }
