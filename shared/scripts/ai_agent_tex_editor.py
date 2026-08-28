from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
BACKUP_ROOT = APP_PATHS.user_data_root / "backups" / "ai_agent_tex_edits"
MAX_INSERT_CHARS = 120_000
PATCH_STATE_NAME = ".ai_agent_tex_patches.json"


@dataclass(slots=True)
class TexEditResult:
    project_code: str
    relative_path: str
    operation: str
    backup_directory: Path
    validation_log: str
    changed_files: list[str]


_FORBIDDEN_TEX = (
    (re.compile(r"\\(?:immediate\s*)?write\s*18\b", re.I), r"\write18"),
    (re.compile(r"\\(?:openout|openin|read|write|newread|newwrite)\b", re.I), "TeX 文件读写原语"),
    (re.compile(r"\\(?:input|include)\s*\{", re.I), r"\input/\include"),
    (re.compile(r"\\(?:directlua|special|catcode|scantokens)\b", re.I), "底层执行原语"),
    (re.compile(r"\\begin\s*\{(?:filecontents\*?|VerbatimOut)\}", re.I), "文件输出环境"),
    (re.compile(r"(?:^|[\s{])run\s*:", re.I), "run: 外部链接"),
)


def _validate_tex_fragment(fragment: str) -> str:
    text = str(fragment or "")
    if not text.strip():
        raise ValueError("要写入的 TeX 内容不能为空。")
    if len(text) > MAX_INSERT_CHARS:
        raise ValueError(f"单次写入内容不能超过 {MAX_INSERT_CHARS} 个字符。")
    for pattern, label in _FORBIDDEN_TEX:
        if pattern.search(text):
            raise ValueError(f"为保证项目安全，AI 写入内容不能包含 {label}。")
    for match in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^}]*)\}", text, re.I):
        source = match.group(1).strip().replace("\\", "/")
        if Path(source).is_absolute() or ".." in Path(source).parts:
            raise ValueError(r"\includegraphics 只能引用项目内的相对路径。")
    return text.rstrip()


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.ai-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _patch_state_path(project_dir: Path) -> Path:
    return project_dir / PATCH_STATE_NAME


def _load_patch_state(project_dir: Path) -> dict[str, Any]:
    path = _patch_state_path(project_dir)
    if not path.is_file():
        return {"schema": 1, "patches": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"AI TeX 持久化补丁文件无法读取：{path}") from error
    if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("patches"), list):
        raise ValueError(f"AI TeX 持久化补丁文件格式无效：{path}")
    return state


def _write_patch_state(project_dir: Path, state: dict[str, Any]) -> None:
    _atomic_write_text(_patch_state_path(project_dir), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _is_generated_project_tex(relative_path: Path) -> bool:
    parts = relative_path.as_posix().split("/")
    return (
        relative_path.as_posix() in {"main.tex", "metadata.tex"}
        or (len(parts) == 2 and parts[0] in {"chapters", "preamble", "notation"})
    )


def _patch_already_applied(text: str, operation: str, anchor: str, new_tex: str) -> bool:
    if operation == "replace":
        return new_tex in text and anchor not in text
    if operation == "insert_before":
        return new_tex + "\n\n" + anchor in text
    if operation == "insert_after":
        return anchor + "\n\n" + new_tex in text
    return False


def reapply_project_tex_patches(project_dir: Path) -> dict[str, Any]:
    """Reapply successful AI edits after generated project TeX files are refreshed."""
    root = Path(project_dir).resolve()
    state = _load_patch_state(root)
    applied: list[str] = []
    unchanged: list[str] = []
    for index, raw in enumerate(state["patches"], start=1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"第 {index} 条 AI TeX 持久化补丁格式无效。")
        relative = Path(str(raw.get("relative_path") or "").replace("\\", "/"))
        target = (root / relative).resolve()
        if relative.is_absolute() or relative.suffix.casefold() != ".tex" or root not in target.parents:
            raise RuntimeError(f"第 {index} 条 AI TeX 补丁路径不安全：{relative}")
        if not target.is_file():
            raise RuntimeError(f"AI TeX 补丁的目标文件不存在：{relative.as_posix()}")
        operation = str(raw.get("operation") or "")
        anchor = str(raw.get("anchor_text") or "")
        new_tex = _validate_tex_fragment(str(raw.get("new_tex") or ""))
        original = target.read_text(encoding="utf-8")
        if _patch_already_applied(original, operation, anchor, new_tex):
            unchanged.append(relative.as_posix())
            continue
        try:
            updated = ProjectTexEditor._apply_operation(original, operation, anchor, new_tex)
        except ValueError as error:
            raise RuntimeError(
                f"无法重新应用 AI TeX 补丁到 {relative.as_posix()}：{error}。"
                "原始定位内容可能已改变；为避免静默丢失图形，已停止项目生成。"
            ) from error
        _atomic_write_text(target, updated)
        applied.append(relative.as_posix())
    return {"applied": applied, "unchanged": unchanged, "patch_count": len(state["patches"])}


def remove_project_tex_patch(project_dir: Path, patch_id: str) -> dict[str, Any]:
    """Remove one persisted AI edit without disturbing later unrelated edits."""
    root = Path(project_dir).resolve()
    state = _load_patch_state(root)
    target_patch = next(
        (
            raw
            for raw in state["patches"]
            if isinstance(raw, dict) and str(raw.get("id") or "") == str(patch_id or "")
        ),
        None,
    )
    if target_patch is None:
        raise ValueError(f"没有找到 AI TeX 补丁：{patch_id}")
    relative = Path(str(target_patch.get("relative_path") or "").replace("\\", "/"))
    target = (root / relative).resolve()
    if relative.is_absolute() or relative.suffix.casefold() != ".tex" or root not in target.parents:
        raise ValueError(f"AI TeX 补丁路径不安全：{relative}")
    if not target.is_file():
        raise FileNotFoundError(f"AI TeX 补丁目标不存在：{relative.as_posix()}")
    operation = str(target_patch.get("operation") or "")
    anchor = str(target_patch.get("anchor_text") or "")
    fragment = _validate_tex_fragment(str(target_patch.get("new_tex") or ""))
    text = target.read_text(encoding="utf-8")
    if operation == "insert_before":
        inserted = fragment + "\n\n" + anchor
        restored = anchor
    elif operation == "insert_after":
        inserted = anchor + "\n\n" + fragment
        restored = anchor
    else:
        raise ValueError("replace 类型补丁不能在没有原文备份的情况下自动撤销。")
    occurrences = text.count(inserted)
    if occurrences != 1:
        raise ValueError(
            f"补丁内容在 {relative.as_posix()} 中出现 {occurrences} 次；"
            "为避免删除无关内容，已停止撤销。"
        )
    _atomic_write_text(target, text.replace(inserted, restored, 1))
    remaining = [raw for raw in state["patches"] if raw is not target_patch]
    if remaining:
        state["patches"] = remaining
        _write_patch_state(root, state)
    else:
        _patch_state_path(root).unlink(missing_ok=True)
    return {
        "removed_patch_id": str(patch_id),
        "relative_path": relative.as_posix(),
        "remaining_patch_count": len(remaining),
    }


class ProjectTexEditor:
    """Controlled project-local TeX edits with backup, validation, and rollback."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def _target(self, subject_name: str, project_ref: str | int, relative_path: str) -> tuple[Path, Any, Path]:
        project_dir, project = self.repository._project_directory(subject_name, project_ref)
        relative = Path(str(relative_path or "").replace("\\", "/"))
        if relative.is_absolute() or relative.suffix.casefold() != ".tex":
            raise ValueError("AI 只能修改学习项目目录内的 .tex 文件。")
        target = (project_dir / relative).resolve()
        if project_dir not in target.parents or not target.is_file():
            raise ValueError("目标 TeX 文件不存在，或路径超出了学习项目目录。")
        return project_dir, project, target

    @staticmethod
    def _backup_directory(subject_name: str, project_code: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_subject = re.sub(r"[^A-Za-z0-9._-]+", "_", subject_name).strip("_") or "subject"
        safe_project = re.sub(r"[^A-Za-z0-9._-]+", "_", project_code).strip("_") or "project"
        path = BACKUP_ROOT / stamp / safe_subject / safe_project
        path.mkdir(parents=True, exist_ok=False)
        return path

    @staticmethod
    def _copy_backup(project_dir: Path, backup_dir: Path, target: Path) -> str:
        relative = target.relative_to(project_dir)
        destination = backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
        return relative.as_posix()

    @staticmethod
    def _validate_project(project_dir: Path) -> str:
        main_tex = project_dir / "main.tex"
        if not main_tex.is_file():
            raise ValueError("项目缺少 main.tex，无法在写入后进行完整编译验证。")
        xelatex = shutil.which("xelatex")
        if not xelatex:
            raise ValueError("未找到 xelatex，不能在安全验证后提交 AI 修改。")
        with tempfile.TemporaryDirectory(prefix="math-problem-bank-ai-tex-") as temporary:
            output_dir = Path(temporary)
            for directory in project_dir.rglob("*"):
                if not directory.is_dir():
                    continue
                resolved = directory.resolve()
                if project_dir not in resolved.parents:
                    continue
                (output_dir / resolved.relative_to(project_dir)).mkdir(parents=True, exist_ok=True)
            command = [
                xelatex,
                "-no-pdf",
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-file-line-error",
                "-halt-on-error",
                f"-output-directory={output_dir}",
                main_tex.name,
            ]
            process = subprocess.run(
                command,
                cwd=project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
            log = process.stdout
            if process.returncode != 0 or not (output_dir / "main.xdv").is_file():
                tail = "\n".join(log.splitlines()[-100:])
                raise RuntimeError("项目 XeLaTeX 验证失败：\n" + tail)
            return "\n".join(log.splitlines()[-30:])

    @staticmethod
    def _project_value(project: Any, key: str, default: Any = "") -> Any:
        try:
            return project[key] if key in project.keys() else default
        except (AttributeError, KeyError, TypeError):
            return project.get(key, default) if isinstance(project, dict) else default

    def _validate_and_publish_project(
        self,
        subject_name: str,
        project_dir: Path,
        project: Any,
    ) -> dict[str, Any]:
        project_id = int(self._project_value(project, "id", 0) or 0)
        project_code = str(self._project_value(project, "collection_code", "") or "")
        pdf_filename = str(
            self._project_value(project, "pdf_filename", "") or f"{project_code}.pdf"
        )
        if project_id <= 0:
            return {
                "validation": "XeLaTeX 编译通过（shell escape 已禁用）",
                "validation_log_tail": self._validate_project(project_dir),
                "project_pdf_path": "",
                "project_pdf_duration_seconds": 0.0,
            }

        # Reuse the application's incremental PDF builder. It preserves aux/XDV
        # caches, performs another pass only when references changed, converts
        # the final PDF, and atomically updates the canonical project PDF.
        from shared.scripts.problem_bank_center_qt import DashboardService, SUBJECTS

        logs: list[str] = []
        result = DashboardService(SUBJECTS).build_current_project_pdf(
            subject_name,
            project_id,
            logs.append,
            clean_build_history=False,
        )
        expected_pdf = (project_dir / pdf_filename).resolve()
        actual_pdf = Path(result.pdf_path).resolve()
        if actual_pdf != expected_pdf:
            if actual_pdf.parent == project_dir and actual_pdf.name.startswith(expected_pdf.stem + "_new_"):
                actual_pdf.unlink(missing_ok=True)
            raise RuntimeError(
                "项目 PDF 正被其他窗口占用，新的 PDF 未能替换正式文件；"
                "为避免出现 TeX 已更新但定位仍打开旧 PDF 的状态，本次修改已回滚。"
            )
        if not expected_pdf.is_file() or expected_pdf.stat().st_size <= 0:
            raise RuntimeError("项目编译没有生成可用的正式 PDF，本次修改已回滚。")
        return {
            "validation": "XeLaTeX 增量编译及正式项目 PDF 更新成功（shell escape 已禁用）",
            "validation_log_tail": "\n".join(logs[-35:]),
            "project_pdf_path": str(expected_pdf),
            "project_pdf_duration_seconds": float(result.duration_seconds),
            "project_pdf_size_bytes": int(result.size_bytes),
        }

    def build_project_pdf(self, subject_name: str, project_ref: str | int) -> dict[str, Any]:
        project_dir, project = self.repository._project_directory(subject_name, project_ref)
        result = self._validate_and_publish_project(subject_name, project_dir, project)
        if not result.get("project_pdf_path"):
            raise RuntimeError("当前项目缺少可发布的正式 PDF 配置。")
        return {
            "changed": False,
            "subject_name": subject_name,
            "project_code": str(self._project_value(project, "collection_code", "") or ""),
            **result,
        }

    @staticmethod
    def _apply_operation(original: str, operation: str, anchor_text: str, new_tex: str) -> str:
        anchor = str(anchor_text or "")
        if not anchor:
            raise ValueError("必须提供从目标文件读取到的精确定位文本 anchor_text。")
        occurrences = original.count(anchor)
        if occurrences != 1:
            raise ValueError(f"定位文本在目标文件中出现 {occurrences} 次；必须提供只出现一次的精确文本。")
        if operation == "insert_before":
            replacement = new_tex + "\n\n" + anchor
        elif operation == "insert_after":
            replacement = anchor + "\n\n" + new_tex
        elif operation == "replace":
            replacement = new_tex
        else:
            raise ValueError("operation 只能是 insert_before、insert_after 或 replace。")
        return original.replace(anchor, replacement, 1)

    def edit_tex(
        self,
        subject_name: str,
        project_ref: str | int,
        relative_path: str,
        operation: str,
        anchor_text: str,
        new_tex: str,
    ) -> dict[str, Any]:
        project_dir, project, target = self._target(subject_name, project_ref, relative_path)
        fragment = _validate_tex_fragment(new_tex)
        original = target.read_text(encoding="utf-8")
        updated = self._apply_operation(original, operation, anchor_text, fragment)
        if updated == original:
            return {
                "changed": False,
                "message": "目标文件已经是请求的内容，没有重复写入。",
                "relative_path": target.relative_to(project_dir).as_posix(),
            }
        project_code = str(project["collection_code"] or "")
        backup_dir = self._backup_directory(subject_name, project_code)
        backed_up = [self._copy_backup(project_dir, backup_dir, target)]
        relative = target.relative_to(project_dir)
        persist_patch = _is_generated_project_tex(relative)
        patch_state_path = _patch_state_path(project_dir)
        previous_patch_state = patch_state_path.read_text(encoding="utf-8") if patch_state_path.is_file() else None
        if previous_patch_state is not None:
            backed_up.append(self._copy_backup(project_dir, backup_dir, patch_state_path))
        try:
            _atomic_write_text(target, updated)
            if persist_patch:
                state = _load_patch_state(project_dir)
                state["patches"].append(
                    {
                        "id": uuid.uuid4().hex,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "relative_path": relative.as_posix(),
                        "operation": operation,
                        "anchor_text": anchor_text,
                        "new_tex": fragment,
                    }
                )
                _write_patch_state(project_dir, state)
            publication = self._validate_and_publish_project(subject_name, project_dir, project)
        except Exception:
            _atomic_write_text(target, original)
            if previous_patch_state is None:
                patch_state_path.unlink(missing_ok=True)
            else:
                _atomic_write_text(patch_state_path, previous_patch_state)
            raise
        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "subject_name": subject_name,
            "project_code": project_code,
            "operation": operation,
            "changed_files": backed_up,
            "persistent_after_project_regeneration": persist_patch,
        }
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "changed": True,
            "subject_name": subject_name,
            "project_code": project_code,
            "relative_path": target.relative_to(project_dir).as_posix(),
            "operation": operation,
            "backup_directory": str(backup_dir),
            **publication,
            "changed_files": backed_up,
            "persistent_after_project_regeneration": persist_patch,
        }

    def insert_tikz_figure(
        self,
        subject_name: str,
        project_ref: str | int,
        relative_path: str,
        anchor_text: str,
        position: str,
        tikz_code: str,
        caption: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        code = str(tikz_code or "").strip()
        if not re.search(r"\\begin\s*\{tikzpicture\}", code):
            raise ValueError(r"tikz_code 必须包含完整的 \begin{tikzpicture}...\end{tikzpicture}。")
        if not re.search(r"\\end\s*\{tikzpicture\}", code):
            raise ValueError(r"tikz_code 缺少 \end{tikzpicture}。")
        if label and not re.fullmatch(r"[A-Za-z0-9:._-]+", label.strip()):
            raise ValueError("图片 label 只能包含字母、数字、冒号、点、下划线和连字符。")
        if r"\begin{figure}" not in code and (caption.strip() or label.strip()):
            lines = [r"\begin{figure}[htbp]", r"\centering", code]
            if caption.strip():
                lines.append(r"\caption{" + caption.strip() + "}")
            if label.strip():
                lines.append(r"\label{" + label.strip() + "}")
            lines.append(r"\end{figure}")
            code = "\n".join(lines)
        operation = "insert_before" if position == "before" else "insert_after" if position == "after" else ""
        return self.edit_tex(subject_name, project_ref, relative_path, operation, anchor_text, code)
