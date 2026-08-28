from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
DEFAULT_LEAN_WORKSPACE = (APP_PATHS.workspace_root / "MathWorkspace" / "LeanProofs").resolve()
GENERATED_LEAN_ROOT = (DEFAULT_LEAN_WORKSPACE / "Generated").resolve()
ELAN_BIN = (Path.home() / ".elan" / "bin").resolve()

_FORBIDDEN_SOURCE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bsorry\b", "sorry 占位证明"),
    (r"\badmit\b", "admit 占位证明"),
    (r"\baxiom\b", "在生成文件中声明新公理"),
    (r"\bopaque\b", "opaque 声明"),
    (r"\bunsafe\b", "unsafe 声明"),
    (r"\bpartial\b", "partial 声明"),
    (r"\brun_tac\b", "run_tac 编译期元编程"),
    (r"\bnative_decide\b", "native_decide 本机代码执行"),
    (r"(?m)^\s*(?:elab|macro|syntax)\b", "自定义语法或 elaborator"),
    (r"#\s*(?:eval|reduce)\b", "编译期求值命令"),
    (r"@\s*\[\s*extern\b", "外部函数绑定"),
    (r"\b(?:IO\.Process|Process\.spawn|System\.FilePath|FilePath\.write)\b", "文件或进程 IO"),
)


def _strip_lean_comments(source: str) -> str:
    """Remove nested block comments and line comments before policy checks."""

    output: list[str] = []
    index = 0
    depth = 0
    in_string = False
    while index < len(source):
        pair = source[index : index + 2]
        char = source[index]
        if depth:
            if pair == "/-":
                depth += 1
                index += 2
            elif pair == "-/":
                depth -= 1
                index += 2
            else:
                if char == "\n":
                    output.append("\n")
                index += 1
            continue
        if not in_string and pair == "/-":
            depth = 1
            index += 2
            continue
        if not in_string and pair == "--":
            newline = source.find("\n", index + 2)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        output.append(char)
        if char == '"' and (index == 0 or source[index - 1] != "\\"):
            in_string = not in_string
        index += 1
    if depth:
        raise ValueError("Lean 源文件包含未闭合的块注释。")
    return "".join(output)


def validate_lean_source(source: str) -> dict[str, Any]:
    text = str(source or "")
    if "\x00" in text:
        raise ValueError("Lean 源文件包含 NUL 字符。")
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("单个 Lean 源文件不能超过 1 MB。")
    stripped = _strip_lean_comments(text)
    for pattern, label in _FORBIDDEN_SOURCE_PATTERNS:
        if re.search(pattern, stripped, flags=re.IGNORECASE):
            raise ValueError(f"Lean 安全与证明完整性检查拒绝了：{label}。")
    imports = re.findall(r"(?m)^\s*import\s+([^\r\n]+)", stripped)
    if not imports:
        raise ValueError("Lean 证明文件必须显式 import Mathlib。")
    imported_modules = [module for line in imports for module in line.split()]
    if not imported_modules or any(
        module != "Mathlib" and not module.startswith("Mathlib.") for module in imported_modules
    ):
        raise ValueError("AI 生成的 Lean 文件只允许导入 Mathlib 或 Mathlib.* 官方模块。")
    declarations = len(
        re.findall(r"(?m)^\s*(?:theorem|lemma|example|def|structure|inductive|class|instance)\b", stripped)
    )
    if declarations == 0:
        raise ValueError("Lean 文件中没有可核验的声明。")
    return {
        "format": "lean",
        "valid": True,
        "imports": imported_modules,
        "declaration_count": declarations,
        "proof_placeholders": False,
        "policy": "mathlib_only_no_sorry_no_axiom_no_elaboration_io",
    }


def _safe_environment() -> dict[str, str]:
    allowed = {
        "APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATHEXT",
        "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "SYSTEMDRIVE",
        "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    path_parts = [str(ELAN_BIN)]
    git_executable = shutil.which("git")
    if git_executable:
        path_parts.append(str(Path(git_executable).resolve().parent))
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    path_parts.extend([str(system_root / "System32"), str(system_root)])
    environment["PATH"] = os.pathsep.join(path_parts)
    environment["ELAN_HOME"] = str(Path.home() / ".elan")
    environment["LEAN_ABORT_ON_PANIC"] = "1"
    return environment


def _hidden_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_hidden_kwargs(),
        )
    else:
        process.kill()


class LeanCheckManager:
    """Run one fixed Lean kernel check inside the managed mathlib project."""

    def __init__(self, project_root: Path = DEFAULT_LEAN_WORKSPACE) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.generated_root = (self.project_root / "Generated").resolve()
        self._current_process: subprocess.Popen[str] | None = None

    def resolve_target(self, raw_path: str) -> Path:
        value = str(raw_path or "").strip().strip("\"'")
        if not value:
            raise ValueError("Lean 文件路径不能为空。")
        target = Path(value).expanduser()
        if not target.is_absolute():
            parts = target.parts
            if parts and parts[0].casefold() == "mathworkspace":
                target = ROOT_DIR / target
            elif parts and parts[0].casefold() == "leanproofs":
                target = self.project_root.parent / target
            else:
                target = self.project_root / target
        target = target.resolve()
        if target.suffix.casefold() != ".lean":
            raise ValueError("lean_check 只接受 .lean 文件。")
        if self.generated_root not in target.parents:
            raise ValueError(f"AI 只能核验受控目录中的文件：{self.generated_root}")
        if not target.is_file():
            raise FileNotFoundError(f"Lean 文件不存在：{target}")
        return target

    def cancel_current(self) -> None:
        if self._current_process is not None:
            _terminate_process(self._current_process)

    def check(
        self,
        raw_path: str,
        progress: Callable[[str], None] | None = None,
        timeout_seconds: int = 90,
    ) -> dict[str, Any]:
        target = self.resolve_target(raw_path)
        source = target.read_text(encoding="utf-8")
        source_validation = validate_lean_source(source)
        timeout = max(10, min(int(timeout_seconds or 90), 180))
        lake = ELAN_BIN / ("lake.exe" if os.name == "nt" else "lake")
        if not lake.is_file():
            raise RuntimeError(f"没有找到 Lake：{lake}。请先安装 Elan/Lean。")
        if not (self.project_root / "lakefile.toml").is_file():
            raise RuntimeError(f"Lean 项目缺少 lakefile.toml：{self.project_root}")
        relative = target.relative_to(self.project_root).as_posix()
        command = [str(lake), "env", "lean", relative]
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            env=_safe_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_hidden_kwargs(),
        )
        self._current_process = process
        register = getattr(progress, "register_cancel", None)
        unregister = register(self.cancel_current) if callable(register) else (lambda: None)
        try:
            try:
                output, _unused = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_process(process)
                output, _unused = process.communicate()
                raise RuntimeError(f"Lean 内核核验超过 {timeout} 秒，已终止。") from None
        finally:
            unregister()
            self._current_process = None
        cancelled = getattr(progress, "is_cancelled", None)
        if callable(cancelled) and cancelled():
            raise RuntimeError("Lean 内核核验已取消。")
        output = str(output or "")
        duration_ms = int((time.monotonic() - started) * 1000)
        verified = process.returncode == 0
        return {
            "verified": verified,
            "verification": "lean_kernel_exit_zero" if verified else "lean_kernel_rejected",
            "exit_code": int(process.returncode or 0),
            "path": str(target),
            "relative_path": relative,
            "project_root": str(self.project_root),
            "duration_ms": duration_ms,
            "diagnostics": output[-20000:],
            "source_validation": source_validation,
            "contains_sorry": False,
            "contains_admit": False,
            "contains_axiom": False,
        }
