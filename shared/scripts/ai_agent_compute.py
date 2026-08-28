from __future__ import annotations

import atexit
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import sympy as sp

from shared.scripts.application_paths import APP_PATHS

ROOT_DIR = APP_PATHS.application_root
PYTHON_WORKER_PATH = Path(__file__).with_name("ai_agent_compute_worker.py")
MATHEMATICA_BRIDGE_PATH = Path(__file__).with_name("ai_agent_mathematica_bridge.py")
DEFAULT_MMA_MCP_DIR = APP_PATHS.mma_mcp_root
DEFAULT_WOLFRAM_KERNEL = APP_PATHS.wolfram_kernel
ARTIFACT_ROOT = (APP_PATHS.cache_dir / "ai_agent_artifacts").resolve()
ARTIFACT_MIME_TYPES = {
    "png": "image/png",
    "pdf": "application/pdf",
    "svg": "image/svg+xml",
    "json": "application/json",
    "wl": "text/plain",
}

PYTHON_COMPUTE_TOOLS = {
    "symbolic_math",
    "numerical_math",
    "verify_formula",
    "find_counterexample",
    "plot_math_function",
}
MATHEMATICA_COMPUTE_TOOLS = {"mathematica_compute", "mathematica_plot"}
DUAL_COMPUTE_TOOLS = {"dual_verify_math"}
LEAN_VERIFY_TOOLS = {"lean_check"}
COMPUTE_TOOL_NAMES = PYTHON_COMPUTE_TOOLS | MATHEMATICA_COMPUTE_TOOLS | DUAL_COMPUTE_TOOLS


@dataclass(slots=True)
class ComputeToolResult:
    success: bool
    engine: str
    operation: str
    input: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    formatted_output: str = ""
    canonical_expression: str = ""
    numeric_approximation: str = ""
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_data(self, legacy: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(legacy or {})
        data.update(asdict(self))
        return data


def _safe_process_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _register_cancel(progress: Callable[[str], None] | None, callback: Callable[[], None]) -> Callable[[], None]:
    register = getattr(progress, "register_cancel", None)
    if callable(register):
        return register(callback)
    return lambda: None


def _artifact_record(path: Path, *, kind: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise RuntimeError(f"计算产物不存在或为空：{resolved}")
    suffix = resolved.suffix.casefold().lstrip(".")
    return {
        "artifact_id": resolved.stem,
        "kind": kind,
        "format": suffix,
        "absolute_path": str(resolved),
        "relative_path": resolved.relative_to(ROOT_DIR).as_posix(),
        "mime_type": ARTIFACT_MIME_TYPES.get(suffix, "application/octet-stream"),
        "size_bytes": resolved.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def resolve_compute_tool_names(mode: str, user_text: str, task_kind: str) -> set[str]:
    selected = str(mode or "auto").casefold()
    if selected == "off":
        return set()
    if selected == "python":
        return set(PYTHON_COMPUTE_TOOLS)
    if selected == "mathematica":
        return set(MATHEMATICA_COMPUTE_TOOLS)
    if selected == "dual":
        return set(DUAL_COMPUTE_TOOLS)

    text = str(user_text or "")

    def negates(name_pattern: str) -> bool:
        return bool(
            re.search(
                rf"(?:不要|禁止|无需|不必|别)\s*(?:再|改)?\s*(?:使用|调用|采用|改用)?\s*.{{0,16}}(?:{name_pattern})",
                text,
                re.I,
            )
        )

    lean_pattern = r"Lean(?:\s*4)?|mathlib|\.lean\b|形式化(?:证明|验证)|内核(?:核验|验证)"
    if re.search(lean_pattern, text, re.I) and not negates(lean_pattern):
        return set(LEAN_VERIFY_TOOLS)

    mathematica_pattern = r"Mathematica|Wolfram|mma-mcp|mathematica_(?:compute|plot)|DSolve|微分方程|特殊函数|复杂积分|复杂求和|闭式"
    python_pattern = r"Python|python_(?:compute|plot)|plot_math_function"
    mathematica_requested = bool(re.search(mathematica_pattern, text, re.I)) and not negates(
        mathematica_pattern
    )
    python_requested = bool(re.search(python_pattern, text, re.I)) and not negates(python_pattern)
    explicit_dual = re.search(
        r"双重(?:核验|验证|检查)|两种(?:工具|软件|引擎).*核验|"
        r"(?:Python.{0,12}(?:和|与|及|以及|同时|分别|对比|比较).{0,12}Mathematica|"
        r"Mathematica.{0,12}(?:和|与|及|以及|同时|分别|对比|比较).{0,12}Python)",
        text,
        re.I,
    )
    if explicit_dual and mathematica_requested and python_requested:
        return set(DUAL_COMPUTE_TOOLS)
    if mathematica_requested:
        return set(MATHEMATICA_COMPUTE_TOOLS)
    if python_requested:
        return set(PYTHON_COMPUTE_TOOLS)
    proof_like = re.search(r"证明|为什么|定义|概念|定理|推导这一步|逻辑|\bproof\b|\bdefinition\b|\btheorem\b", text, re.I)
    explicit_compute = re.search(r"计算|求值|化简|展开|因式分解|求导|积分|极限|求和|级数|方程|矩阵|特征值|行列式|误差|数值|验证|反例|\bcalculate\b|\bcompute\b|\bsimplify\b|\bintegrate\b|\blimit\b|\bsolve\b|\bseries\b|\bmatrix\b|\beigenvalue", text, re.I)
    if proof_like and not explicit_compute:
        return set()
    if task_kind == "drawing_or_visualization" and re.search(
        r"三维|3D|曲面|隐式|等高线|区域图|向量场|空间曲线|参数曲面|Contour|RegionPlot|Plot3D|VectorPlot",
        text,
        re.I,
    ):
        return set(PYTHON_COMPUTE_TOOLS) | {"mathematica_plot"}
    if explicit_compute or task_kind == "drawing_or_visualization":
        return set(PYTHON_COMPUTE_TOOLS)
    return set()


class MathematicaBridgeProcess:
    def __init__(self, mma_mcp_dir: Path = DEFAULT_MMA_MCP_DIR) -> None:
        self.mma_mcp_dir = Path(mma_mcp_dir)
        self.python_executable = self.mma_mcp_dir / ".venv" / "Scripts" / "python.exe"
        self.kernel_path = DEFAULT_WOLFRAM_KERNEL
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: list[str] = []
        self._lock = threading.Lock()

    def _reader(self, stream: Any) -> None:
        for raw in iter(stream.readline, ""):
            line = str(raw or "").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("id"):
                self._responses.put(parsed)

    def _stderr_reader(self, stream: Any) -> None:
        for raw in iter(stream.readline, ""):
            line = str(raw or "").strip()
            if line:
                self._stderr_tail.append(line)
                del self._stderr_tail[:-40]

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not self.python_executable.is_file():
            raise RuntimeError(f"mma-mcp Python 环境不存在：{self.python_executable}")
        if not self.kernel_path.is_file():
            raise RuntimeError(f"WolframKernel 不存在：{self.kernel_path}")
        env = _safe_process_environment()
        env["WOLFRAM_KERNEL"] = str(self.kernel_path)
        self._process = subprocess.Popen(
            [str(self.python_executable), str(MATHEMATICA_BRIDGE_PATH)],
            cwd=str(self.mma_mcp_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=_creation_flags(),
        )
        assert self._process.stdout is not None and self._process.stderr is not None
        threading.Thread(target=self._reader, args=(self._process.stdout,), daemon=True).start()
        threading.Thread(target=self._stderr_reader, args=(self._process.stderr,), daemon=True).start()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                process.stdin.flush()
            process.wait(timeout=2)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=3)
            except Exception:
                pass

    def request(
        self,
        payload: dict[str, Any],
        *,
        timeout: int,
        progress: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        with self._lock:
            request_id = uuid.uuid4().hex
            request = {"id": request_id, **payload}
            unregister = _register_cancel(progress, self.close)
            try:
                for attempt in range(2):
                    self.start()
                    process = self._process
                    if process is None or process.stdin is None:
                        raise RuntimeError("Mathematica 桥接进程未启动。")
                    try:
                        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                        process.stdin.flush()
                        deadline = time.monotonic() + max(1, int(timeout))
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                raise RuntimeError("Mathematica 桥接进程意外退出。")
                            try:
                                response = self._responses.get(timeout=min(0.2, deadline - time.monotonic()))
                            except queue.Empty:
                                continue
                            if str(response.get("id") or "") == request_id:
                                response.setdefault("restarted", bool(attempt))
                                return response
                        raise TimeoutError(f"Mathematica 计算超过 {timeout} 秒。")
                    except (BrokenPipeError, OSError, RuntimeError, TimeoutError):
                        self.close()
                        if attempt == 0:
                            if progress:
                                progress("Mathematica 桥接中断，正在自动重启一次…")
                            continue
                        detail = "\n".join(self._stderr_tail[-8:])
                        raise RuntimeError((detail or "Mathematica 桥接重启后仍未成功。")[:1800])
                raise RuntimeError("Mathematica 桥接请求未完成。")
            finally:
                unregister()


class LocalComputeManager:
    def __init__(self) -> None:
        self._active_python: subprocess.Popen[str] | None = None
        self._python_lock = threading.Lock()
        self.mathematica = MathematicaBridgeProcess()
        atexit.register(self.close)

    def close(self) -> None:
        self.cancel_current()
        self.mathematica.close()

    def cancel_current(self) -> None:
        process = self._active_python
        if process is not None and process.poll() is None:
            process.kill()
        self.mathematica.close()

    def run_python(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        progress: Callable[[str], None] | None = None,
        timeout: int = 20,
    ) -> dict[str, Any]:
        if tool_name not in PYTHON_COMPUTE_TOOLS:
            raise ValueError(f"不支持的 Python 计算工具：{tool_name}")
        started = time.perf_counter()
        with self._python_lock:
            artifact_dir = ""
            if tool_name == "plot_math_function":
                artifact_dir = str((ARTIFACT_ROOT / f"run_{uuid.uuid4().hex}").resolve())
            process = subprocess.Popen(
                [sys.executable, "-m", "shared.scripts.ai_agent_compute_worker"],
                cwd=str(ROOT_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_safe_process_environment(),
                creationflags=_creation_flags(),
            )
            self._active_python = process
            unregister = _register_cancel(progress, process.kill)
            try:
                stdout, stderr = process.communicate(
                    json.dumps(
                        {"tool": tool_name, "arguments": arguments, "artifact_dir": artifact_dir},
                        ensure_ascii=False,
                    ),
                    timeout=max(1, int(timeout)),
                )
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise RuntimeError(f"Python 计算超过 {timeout} 秒，已终止计算进程。") from None
            finally:
                unregister()
                self._active_python = None
            if process.returncode != 0:
                raise RuntimeError((stderr.strip() or "Python 计算进程异常退出。")[:1800])
            try:
                response = json.loads(stdout)
            except json.JSONDecodeError:
                raise RuntimeError("Python 计算进程返回了无效 JSON。") from None
        if not isinstance(response, dict) or not response.get("success"):
            raise RuntimeError(str(response.get("error") if isinstance(response, dict) else "Python 计算失败。"))
        legacy = dict(response.get("result") or {})
        raw_output = str(legacy.get("result") or legacy.get("difference") or legacy)
        formatted = str(legacy.get("result_latex") or legacy.get("difference_latex") or raw_output)
        result = ComputeToolResult(
            success=True,
            engine="python",
            operation=str(arguments.get("operation") or tool_name),
            input=dict(arguments),
            raw_output=raw_output[:65536],
            formatted_output=formatted[:65536],
            canonical_expression=str(legacy.get("canonical_expression") or legacy.get("result") or ""),
            numeric_approximation=str(legacy.get("numeric_approximation") or ""),
            assumptions=[dict(item) for item in arguments.get("assumptions") or [] if isinstance(item, dict)],
            conditions=[str(item) for item in legacy.get("conditions") or []],
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            artifacts=[dict(item) for item in legacy.get("artifacts") or [] if isinstance(item, dict)],
            metadata={
                "worker": "isolated_subprocess",
                "verification": legacy.get("verification"),
                "warnings": list(legacy.get("warnings") or []),
                "plot_metadata": dict(legacy.get("plot_metadata") or {}),
            },
        )
        return result.as_data(legacy)

    def run_mathematica(
        self,
        arguments: dict[str, Any],
        progress: Callable[[str], None] | None = None,
        timeout: int = 70,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        response = self.mathematica.request(
            {"command": "compute", "arguments": arguments}, timeout=timeout, progress=progress
        )
        if not response.get("success"):
            raise RuntimeError(str(response.get("error") or "Mathematica 计算失败。"))
        raw = str(response.get("raw_output") or "")
        canonical = ""
        try:
            from sympy.parsing.mathematica import parse_mathematica

            canonical = str(parse_mathematica(raw))
        except Exception:
            canonical = ""
        result = ComputeToolResult(
            success=True,
            engine="mathematica",
            operation=str(arguments.get("operation") or ""),
            input=dict(arguments),
            raw_output=raw[:65536],
            formatted_output=str(response.get("formatted_output") or raw)[:65536],
            canonical_expression=canonical,
            numeric_approximation=str(response.get("numeric_approximation") or ""),
            assumptions=[dict(item) for item in arguments.get("assumptions") or [] if isinstance(item, dict)],
            conditions=[str(item) for item in response.get("conditions") or []],
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            metadata={
                "bridge": "mma_mcp_isolated_bridge",
                "mma_mcp_version": response.get("mma_mcp_version"),
                "restarted": bool(response.get("restarted")),
            },
        )
        return result.as_data()

    def run_mathematica_plot(
        self,
        arguments: dict[str, Any],
        progress: Callable[[str], None] | None = None,
        timeout: int = 90,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        artifact_dir = (ARTIFACT_ROOT / f"run_{uuid.uuid4().hex}").resolve()
        if progress:
            progress("Mathematica 正在生成并导出图形…")
        response = self.mathematica.request(
            {
                "command": "plot",
                "arguments": dict(arguments),
                "artifact_dir": str(artifact_dir),
            },
            timeout=timeout,
            progress=progress,
        )
        if not response.get("success"):
            raise RuntimeError(str(response.get("error") or "Mathematica 绘图失败。"))
        paths: dict[str, str] = {}
        artifacts: list[dict[str, Any]] = []
        for key, kind in (
            ("png_path", "math_plot"),
            ("pdf_path", "math_plot"),
            ("svg_path", "math_plot"),
            ("source_path", "wolfram_source"),
            ("metadata_path", "plot_metadata"),
        ):
            raw_path = str(response.get(key) or "")
            if not raw_path:
                continue
            path = Path(raw_path).resolve()
            if path.parent != artifact_dir:
                raise RuntimeError(f"Mathematica 返回了受控产物目录之外的路径：{path}")
            paths[key] = str(path)
            artifacts.append(_artifact_record(path, kind=kind))
        if "png_path" not in paths:
            raise RuntimeError("Mathematica 绘图没有生成可预览的 PNG。")
        metadata = dict(response.get("plot_metadata") or {})
        warnings = [str(item) for item in response.get("warnings") or []]
        result = ComputeToolResult(
            success=True,
            engine="mathematica",
            operation=str(arguments.get("plot_type") or "mathematica_plot"),
            input=dict(arguments),
            raw_output=str(response.get("raw_output") or "Mathematica 图形已生成。")[:65536],
            formatted_output=str(response.get("formatted_output") or "Mathematica 图形已生成。")[:65536],
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            artifacts=artifacts,
            metadata={
                "bridge": "mma_mcp_isolated_bridge",
                "mma_mcp_version": response.get("mma_mcp_version"),
                "restarted": bool(response.get("restarted")),
                "verification": "mma_mcp_mathematica_static_plot",
                "warnings": warnings,
                "plot_metadata": metadata,
            },
        )
        return result.as_data(
            {
                **paths,
                "verification": "mma_mcp_mathematica_static_plot",
                "warnings": warnings,
                "plot_metadata": metadata,
            }
        )

    def dual_verify(
        self,
        arguments: dict[str, Any],
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        operation = str(arguments.get("operation") or "simplify")
        python_arguments = dict(arguments)
        python_arguments["operation"] = operation
        parameters = dict(python_arguments.pop("parameters", {}) or {})
        python_arguments.pop("precision", None)
        for key in ("variable", "lower", "upper", "point"):
            if key in parameters and key not in python_arguments:
                python_arguments[key] = parameters[key]
        try:
            python_result = self.run_python("symbolic_math", python_arguments, progress)
        except Exception as error:
            try:
                fallback_mathematica = self.run_mathematica(arguments, progress)
                mathematica_error = ""
            except Exception as second_error:
                fallback_mathematica = {}
                mathematica_error = str(second_error)
            return {
                "success": True,
                "engine": "dual",
                "operation": operation,
                "input": dict(arguments),
                "status": "python_failed",
                "python_error": str(error),
                "mathematica": fallback_mathematica,
                "mathematica_error": mathematica_error,
                "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "raw_output": "python_failed",
                "formatted_output": "Python 计算失败，Mathematica 结果仅供单边参考。",
                "canonical_expression": "",
                "numeric_approximation": "",
                "assumptions": list(arguments.get("assumptions") or []),
                "conditions": [],
                "error": str(error),
                "artifacts": [],
                "metadata": {},
            }
        try:
            mathematica_result = self.run_mathematica(arguments, progress)
        except Exception as error:
            return {
                "success": True,
                "engine": "dual",
                "operation": operation,
                "input": dict(arguments),
                "status": "mathematica_failed",
                "python": python_result,
                "mathematica_error": str(error),
                "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "raw_output": "mathematica_failed",
                "formatted_output": "Mathematica 计算失败，Python 结果仅供单边参考。",
                "canonical_expression": "",
                "numeric_approximation": "",
                "assumptions": list(arguments.get("assumptions") or []),
                "conditions": [],
                "error": str(error),
                "artifacts": [],
                "metadata": {},
            }
        if python_result.get("conditions") or mathematica_result.get("conditions"):
            status = "inconclusive"
        else:
            try:
                left = sp.sympify(str(python_result.get("canonical_expression") or ""))
                right = sp.sympify(str(mathematica_result.get("canonical_expression") or ""))
                difference = sp.simplify(left - right)
                if difference == 0:
                    status = "equivalent"
                elif not difference.free_symbols:
                    status = "not_equivalent"
                else:
                    samples = []
                    consistent = True
                    for value in (-2.0, -0.5, 0.5, 2.0):
                        substitutions = {symbol: value for symbol in difference.free_symbols}
                        try:
                            numeric = complex(sp.N(difference.subs(substitutions), 30))
                        except Exception:
                            continue
                        if math.isfinite(numeric.real) and math.isfinite(numeric.imag):
                            samples.append(value)
                            if abs(numeric) > 1e-10:
                                consistent = False
                                break
                    status = "numerically_consistent" if samples and consistent else "not_equivalent" if samples else "inconclusive"
            except Exception:
                status = "inconclusive"
        return {
            "success": True,
            "engine": "dual",
            "operation": operation,
            "input": dict(arguments),
            "status": status,
            "python": python_result,
            "mathematica": mathematica_result,
            "warning": (
                "有限数值采样一致不构成严格等价证明。"
                if status == "numerically_consistent"
                else ""
            ),
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "raw_output": status,
            "formatted_output": status,
            "canonical_expression": "",
            "numeric_approximation": "",
            "assumptions": list(arguments.get("assumptions") or []),
            "conditions": [],
            "error": "",
            "artifacts": [],
            "metadata": {},
        }
