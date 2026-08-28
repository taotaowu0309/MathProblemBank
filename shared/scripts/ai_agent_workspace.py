from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import sympy as sp

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_math import _run_cancellable_process


ROOT_DIR = APP_PATHS.application_root
DEFAULT_MATH_WORKSPACE = APP_PATHS.workspace_root / "MathWorkspace"
DEFAULT_PHYSICS_WORKSPACE = APP_PATHS.workspace_root / "PhysicsWorkspace"
BACKUP_ROOT = APP_PATHS.user_data_root / "backups" / "ai_math_workspace"
FIGURE_PREVIEW_ROOT = APP_PATHS.cache_dir / "ai_figure_previews"
WRITABLE_SUFFIXES = {
    ".tex", ".md", ".txt", ".csv", ".tsv", ".json", ".bib", ".sty", ".cls",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".htm", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sql", ".ps1", ".sh",
    ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".java", ".rs", ".go", ".r", ".m", ".lean",
}
_SAFE_PLOT_EXPRESSION = re.compile(r"^[0-9xX+\-*/().^\s]+$")
_SAFE_SURFACE_EXPRESSION = re.compile(r"^[0-9xXyY+\-*/().^\s]+$")


def validate_tikz_math(tikz_code: str) -> dict[str, Any]:
    """Check numeric markers against simple 2D functions without executing TeX code."""

    source = str(tikz_code or "")
    x = sp.Symbol("x", real=True)
    y = sp.Symbol("y", real=True)
    expressions: list[sp.Expr] = []
    for raw in re.findall(r"\\addplot(?:\s*\[[^]]*\])?\s*\{([^{}]+)\}\s*;", source):
        candidate = str(raw).strip()
        if not _SAFE_PLOT_EXPRESSION.fullmatch(candidate):
            continue
        try:
            expression = sp.sympify(candidate.replace("^", "**"), locals={"x": x, "X": x})
        except (sp.SympifyError, TypeError, ValueError):
            continue
        if expression.free_symbols <= {x}:
            expressions.append(sp.simplify(expression))

    surfaces: list[sp.Expr] = []
    for raw in re.findall(r"\\addplot3(?:\s*\[[^]]*\])?\s*\{([^{}]+)\}\s*;", source):
        candidate = str(raw).strip()
        if not _SAFE_SURFACE_EXPRESSION.fullmatch(candidate):
            continue
        try:
            surface = sp.sympify(
                candidate.replace("^", "**"),
                locals={"x": x, "X": x, "y": y, "Y": y},
            )
        except (sp.SympifyError, TypeError, ValueError):
            continue
        if surface.free_symbols <= {x, y}:
            surfaces.append(sp.simplify(surface))

    coordinates: list[tuple[float, float]] = []
    marker_blocks = re.findall(
        r"\\addplot\s*\[([^]]*)\]\s*coordinates\s*\{([^}]*)\}",
        source,
        flags=re.DOTALL,
    )
    for options, block in marker_blocks:
        lowered_options = options.casefold()
        if "only marks" not in lowered_options and "mark=" not in lowered_options:
            continue
        for raw_x, raw_y in re.findall(
            r"\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\)", block
        ):
            coordinates.append((float(raw_x), float(raw_y)))

    issues: list[str] = []
    checked: list[dict[str, Any]] = []
    if expressions:
        for point_x, point_y in coordinates[:80]:
            residuals: list[float] = []
            for expression in expressions:
                try:
                    residuals.append(abs(float(sp.N(expression.subs(x, point_x))) - point_y))
                except (TypeError, ValueError, OverflowError):
                    continue
            if not residuals:
                continue
            residual = min(residuals)
            checked.append({"point": [point_x, point_y], "minimum_residual": round(residual, 8)})
            tolerance = max(1e-5, abs(point_y) * 2e-5)
            if residual > tolerance:
                issues.append(
                    f"标记点 ({point_x:g}, {point_y:g}) 不在所绘函数上；最小纵坐标误差为 {residual:.6g}。"
                )

    extrema: list[dict[str, Any]] = []
    for options, point_x, point_y, label in re.findall(
        r"\\node\s*\[([^]]*)\]\s*at\s*\(axis cs:\s*([-+]?\d+(?:\.\d+)?)\s*,\s*"
        r"([-+]?\d+(?:\.\d+)?)\s*\)\s*\{([^{}]*)\}\s*;",
        source,
        flags=re.DOTALL,
    ):
        kind = ""
        lowered = (options + " " + label).casefold()
        if "local max" in lowered or "极大" in lowered:
            kind = "maximum"
        elif "local min" in lowered or "极小" in lowered:
            kind = "minimum"
        if not kind or not expressions:
            continue
        px, py = float(point_x), float(point_y)
        best = min(expressions, key=lambda expr: abs(float(sp.N(expr.subs(x, px))) - py))
        first = float(sp.N(sp.diff(best, x).subs(x, px)))
        second = float(sp.N(sp.diff(best, x, 2).subs(x, px)))
        valid = abs(first) <= 1e-4 and ((kind == "maximum" and second < 0) or (kind == "minimum" and second > 0))
        extrema.append(
            {"point": [px, py], "kind": kind, "first_derivative": first, "second_derivative": second, "valid": valid}
        )
        if not valid:
            issues.append(f"点 ({px:g}, {py:g}) 的极值标注与导数核验不一致。")

    def axis_range(key: str, default: tuple[float, float]) -> tuple[float, float]:
        match = re.search(
            rf"\b{re.escape(key)}\s*=\s*([-+]?\d+(?:\.\d+)?)\s*:\s*([-+]?\d+(?:\.\d+)?)",
            source,
        )
        if not match:
            return default
        lower, upper = float(match.group(1)), float(match.group(2))
        return (lower, upper) if lower < upper else default

    domain_x = axis_range("domain", (-1.0, 1.0))
    domain_y = axis_range("domain y", (-1.0, 1.0))
    surface_samples: list[dict[str, float]] = []
    for surface in surfaces:
        for px in (domain_x[0], (domain_x[0] + domain_x[1]) / 2, domain_x[1]):
            for py in (domain_y[0], (domain_y[0] + domain_y[1]) / 2, domain_y[1]):
                try:
                    value = float(sp.N(surface.subs({x: px, y: py})))
                except (TypeError, ValueError, OverflowError):
                    issues.append(f"三维曲面在 ({px:g}, {py:g}) 处无法得到有限实数值。")
                    continue
                if not sp.Float(value).is_finite:
                    issues.append(f"三维曲面在 ({px:g}, {py:g}) 处不是有限实数。")
                    continue
                surface_samples.append({"x": px, "y": py, "z": value})

    checked_3d_points: list[dict[str, Any]] = []
    for options, block in re.findall(
        r"\\addplot3\s*\[([^]]*)\]\s*coordinates\s*\{([^}]*)\}",
        source,
        flags=re.DOTALL,
    ):
        if "only marks" not in options.casefold() and "mark=" not in options.casefold():
            continue
        for raw_x, raw_y, raw_z in re.findall(
            r"\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*,\s*"
            r"([-+]?\d+(?:\.\d+)?)\s*\)",
            block,
        ):
            px, py, pz = float(raw_x), float(raw_y), float(raw_z)
            residuals = [abs(float(sp.N(surface.subs({x: px, y: py}))) - pz) for surface in surfaces]
            if not residuals:
                continue
            residual = min(residuals)
            checked_3d_points.append(
                {"point": [px, py, pz], "minimum_residual": round(residual, 8)}
            )
            if residual > max(1e-5, abs(pz) * 2e-5):
                issues.append(
                    f"三维标记点 ({px:g}, {py:g}, {pz:g}) 不在所绘曲面上；误差为 {residual:.6g}。"
                )

    sample_match = re.search(r"\bsamples\s*=\s*(\d+)", source)
    sample_y_match = re.search(r"\bsamples y\s*=\s*(\d+)", source)
    if surfaces and sample_match:
        sample_x = int(sample_match.group(1))
        sample_y = int(sample_y_match.group(1)) if sample_y_match else sample_x
        if sample_x * sample_y > 900:
            issues.append(
                f"三维采样网格为 {sample_x}×{sample_y}，超过受控上限 900 个网格点，可能导致 TeX 内存或等待时间异常。"
            )

    return {
        "applicable": bool(expressions or surfaces),
        "passed": not issues,
        "curve_expressions": [str(expression) for expression in expressions],
        "surface_expressions": [str(surface) for surface in surfaces],
        "checked_points": checked,
        "checked_extrema": extrema,
        "checked_3d_points": checked_3d_points,
        "surface_sample_count": len(surface_samples),
        "surface_sample_range": {
            "minimum_z": min((item["z"] for item in surface_samples), default=None),
            "maximum_z": max((item["z"] for item in surface_samples), default=None),
        },
        "issues": issues,
        "verification": "local_symbolic_coordinate_checks",
        "limitations": "自动核验可安全解析的显式二维函数、二维/三维数值标记点、极值标签和显式三维曲面的有限抽样；参数曲线与观察角度仍需视觉检查。",
    }


def _hidden_kwargs() -> dict[str, Any]:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class MathWorkspaceEditor:
    """Transactional edits for local text/source files after user confirmation."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        *,
        default_workspace: Path = DEFAULT_MATH_WORKSPACE,
        allow_lean: bool = True,
    ) -> None:
        self.default_workspace = Path(default_workspace).expanduser().resolve()
        self.allow_lean = bool(allow_lean)
        configured = list(roots or [self.default_workspace, ROOT_DIR, Path.home()])
        if roots is None and os.name == "nt":
            configured.extend(
                Path(f"{letter}:\\")
                for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if Path(f"{letter}:\\").exists()
            )
        self.roots = [Path(root).expanduser().resolve() for root in configured]

    def _target(self, raw_path: str, *, allow_create: bool) -> Path:
        value = str(raw_path or "").strip().strip("\"'")
        if not value:
            raise ValueError("文件路径不能为空。")
        target = Path(value).expanduser()
        if not target.is_absolute():
            if target.parts and target.parts[0].casefold() == self.default_workspace.name.casefold():
                target = Path(*target.parts[1:])
            target = self.default_workspace / target
        target = target.resolve()
        if target.suffix.casefold() not in WRITABLE_SUFFIXES:
            raise ValueError("当前事务写入工具只支持文本、配置、LaTeX 和常见源代码文件；二进制文件需要专用工具。")
        if target.suffix.casefold() == ".lean":
            if not self.allow_lean:
                raise ValueError("物理工作区不支持 Lean 文件；请使用 Python、Mathematica、LaTeX 或普通文本文件。")
            from shared.scripts.ai_agent_lean import GENERATED_LEAN_ROOT

            if GENERATED_LEAN_ROOT not in target.parents:
                raise ValueError(f"AI 生成的 Lean 文件只能写入受控目录：{GENERATED_LEAN_ROOT}")
        matching_root = next((root for root in self.roots if root == target or root in target.parents), None)
        if matching_root is None:
            raise ValueError("目标文件不在受控数学工作区中。")
        if not allow_create and not target.is_file():
            raise FileNotFoundError(f"目标文件不存在：{target}")
        return target

    @staticmethod
    def _apply(original: str, edit: dict[str, Any]) -> str:
        operation = str(edit.get("operation") or "")
        new_text = str(edit.get("new_text") or "")
        if operation == "create_or_replace":
            return new_text
        anchor = str(edit.get("anchor_text") or "")
        if not anchor:
            raise ValueError("非整文件操作必须提供从目标文件读取到的 anchor_text。")
        occurrences = original.count(anchor)
        if occurrences != 1:
            raise ValueError(f"anchor_text 在目标文件中出现 {occurrences} 次，无法安全定位。")
        if operation == "insert_before":
            replacement = new_text + "\n" + anchor
        elif operation == "insert_after":
            replacement = anchor + "\n" + new_text
        elif operation == "replace":
            replacement = new_text
        else:
            raise ValueError("operation 只能是 create_or_replace、insert_before、insert_after 或 replace。")
        return original.replace(anchor, replacement, 1)

    @staticmethod
    def _validate_content(path: Path, text: str) -> dict[str, Any]:
        suffix = path.suffix.casefold()
        if "\x00" in text:
            raise ValueError("文件内容包含 NUL 字符。")
        if len(text) > 5_000_000:
            raise ValueError("单个文件超过 500 万字符限制。")
        if suffix == ".json":
            json.loads(text)
            return {"format": "json", "valid": True}
        if suffix in {".csv", ".tsv"}:
            delimiter = "," if suffix == ".csv" else "\t"
            rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
            if len(rows) > 100000:
                raise ValueError("数据文件行数超过 100000。")
            widths = {len(row) for row in rows if row}
            return {"format": suffix[1:], "valid": True, "rows": len(rows), "column_widths": sorted(widths)}
        if suffix == ".tex":
            if text.count("{") != text.count("}"):
                raise ValueError("TeX 文本的 {} 数量不平衡。")
            forbidden = re.findall(r"\\(?:write18|openout|read|input)\b", text)
            if forbidden:
                raise ValueError("TeX 包含不允许的文件或命令执行指令。")
            return {"format": "tex", "valid": True}
        if suffix == ".lean":
            from shared.scripts.ai_agent_lean import validate_lean_source

            return validate_lean_source(text)
        return {"format": suffix[1:], "valid": True}

    def apply_transaction(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        if not edits or len(edits) > 12:
            raise ValueError("一次跨文件事务必须包含 1 到 12 个编辑。")
        targets: list[tuple[Path, dict[str, Any], str, str]] = []
        existed_before: dict[Path, bool] = {}
        seen: set[str] = set()
        for edit in edits:
            operation = str(edit.get("operation") or "")
            target = self._target(str(edit.get("path") or ""), allow_create=operation == "create_or_replace")
            key = str(target).casefold()
            if key in seen:
                raise ValueError("同一事务不能重复编辑同一个文件；请合并成一次编辑。")
            seen.add(key)
            existed_before[target] = target.is_file()
            original = target.read_text(encoding="utf-8") if existed_before[target] else ""
            updated = self._apply(original, edit)
            self._validate_content(target, updated)
            targets.append((target, dict(edit), original, updated))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = BACKUP_ROOT / timestamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        manifest: list[dict[str, Any]] = []
        try:
            for index, (target, edit, original, updated) in enumerate(targets, 1):
                existed = existed_before[target]
                backup_path = backup_dir / f"{index:02d}_{target.name}"
                if existed:
                    shutil.copy2(target, backup_path)
                _atomic_write(target, updated)
                manifest.append(
                    {
                        "path": str(target),
                        "operation": str(edit.get("operation") or ""),
                        "existed_before": existed,
                        "backup_path": str(backup_path) if existed else "",
                    }
                )
        except Exception:
            for target, _edit, original, _updated in reversed(targets):
                if existed_before.get(target, False):
                    _atomic_write(target, original)
                else:
                    target.unlink(missing_ok=True)
            raise
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "changed": True,
            "changed_files": [str(target) for target, _edit, _original, _updated in targets],
            "changed_file_count": len(targets),
            "backup_directory": str(backup_dir),
            "transaction_verified": True,
            "formats": [self._validate_content(target, updated) for target, _edit, _original, updated in targets],
        }

    def compile_standalone_tex(
        self, path: str, progress: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        target = self._target(path, allow_create=False)
        if target.suffix.casefold() != ".tex":
            raise ValueError("独立文档编译只支持 .tex 文件。")
        text = target.read_text(encoding="utf-8")
        if "\\documentclass" not in text or "\\begin{document}" not in text:
            raise ValueError("该 TeX 是片段而不是可独立编译的完整文档。")
        with tempfile.TemporaryDirectory(prefix="ai_math_document_") as temporary:
            output = Path(temporary)
            command = [
                "xelatex",
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-output-directory={output}",
                str(target),
            ]
            process = _run_cancellable_process(
                command,
                cwd=target.parent,
                timeout=300,
                progress=progress,
            )
            pdf = output / (target.stem + ".pdf")
            if process.returncode != 0 or not pdf.is_file():
                raise RuntimeError("独立 TeX 文档编译失败：\n" + "\n".join(process.stdout.splitlines()[-80:]))
            published = target.with_suffix(".pdf")
            temporary_pdf = published.with_suffix(".pdf.tmp")
            shutil.copy2(pdf, temporary_pdf)
            os.replace(temporary_pdf, published)
            return {
                "tex_path": str(target),
                "pdf_path": str(published),
                "pdf_size_bytes": published.stat().st_size,
                "validation": "XeLaTeX 编译成功，shell escape 已禁用",
                "log_tail": "\n".join(process.stdout.splitlines()[-30:]),
            }

    def render_math_figure_preview(
        self,
        tikz_code: str,
        caption: str = "",
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        fragment = str(tikz_code or "").strip()
        if "\\begin{tikzpicture}" not in fragment or "\\end{tikzpicture}" not in fragment:
            raise ValueError("临时绘图预览必须提供完整的 tikzpicture 环境。")
        if len(fragment) > 120000:
            raise ValueError("TikZ 预览代码超过 120000 字符限制。")
        if re.search(r"\\(?:write18|openout|read|input|includegraphics)\b|\\addplot\s+table\b", fragment):
            raise ValueError("临时绘图预览禁止执行命令或读取外部文件。")
        for left, right in (("{", "}"), ("[", "]"), ("(", ")")):
            if fragment.count(left) != fragment.count(right):
                raise ValueError(f"TikZ 代码的 {left}{right} 数量不平衡。")
        safe_caption = str(caption or "").strip()[:1000]
        source = r"""\documentclass[12pt]{article}
% ai-math-figure-template-v2
\usepackage[margin=16mm]{geometry}
\usepackage{fontspec}
\usepackage{xeCJK}
\usepackage{amsmath,amssymb}
\usepackage{xcolor}
\usepackage{tikz}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}
\usetikzlibrary{arrows.meta,calc,intersections,positioning,decorations.pathreplacing}
\setmainfont{TeX Gyre Termes}
\IfFontExistsTF{Microsoft YaHei}{
  \setCJKmainfont{Microsoft YaHei}
  \setCJKsansfont{Microsoft YaHei}
}{
  \setCJKmainfont{FandolSong-Regular}
  \setCJKsansfont{FandolHei-Regular}
}
\XeTeXlinebreaklocale "zh"
\pagestyle{empty}
\begin{document}
\begin{center}
""" + fragment + ("\n\n" + safe_caption if safe_caption else "") + "\n\\end{center}\n\\end{document}\n"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
        output = FIGURE_PREVIEW_ROOT / digest
        output.mkdir(parents=True, exist_ok=True)
        source_path = output / "figure.tex"
        pdf_path = output / "figure.pdf"
        source_path.write_text(source, encoding="utf-8")
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            process = _run_cancellable_process(
                [
                    "xelatex",
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    f"-output-directory={output}",
                    str(source_path),
                ],
                cwd=output,
                timeout=300,
                progress=progress,
            )
            if process.returncode != 0 or not pdf_path.is_file():
                raise RuntimeError("临时 TikZ 图形编译失败：\n" + "\n".join(process.stdout.splitlines()[-80:]))
            log_tail = "\n".join(process.stdout.splitlines()[-30:])
        else:
            log_tail = "使用已有的临时绘图缓存。"
        from shared.scripts.ai_agent_visual_validation import validate_math_figure

        expected_labels: list[str] = []
        for raw_label in re.findall(r"\\node(?:\s*\[[^]]*\])?[^{}]*\{([^{}]{1,80})\}", fragment):
            label = re.sub(r"\\[A-Za-z]+", "", raw_label)
            label = re.sub(r"[$\\{}]", "", label).strip()
            if len(label) >= 2 and re.search(r"[A-Za-z\u4e00-\u9fff]", label):
                expected_labels.append(label)
        visual = validate_math_figure(str(pdf_path), expected_labels=expected_labels[:20])
        mathematical = validate_tikz_math(fragment)
        return {
            "tex_path": str(source_path),
            "pdf_path": str(pdf_path),
            "pdf_size_bytes": pdf_path.stat().st_size,
            "visual_validation": visual,
            "math_validation": mathematical,
            "validation": "临时 TikZ 已用 XeLaTeX 编译并完成视觉检查；未修改正式项目。",
            "log_tail": log_tail,
            "transient_preview": True,
        }
