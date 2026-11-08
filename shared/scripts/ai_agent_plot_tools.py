from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_math_tools import _local_dict, _parse, _safe_expression


ROOT_DIR = APP_PATHS.application_root
ARTIFACT_ROOT = (APP_PATHS.cache_dir / "ai_agent_artifacts").resolve()
ALLOWED_COLORS = {"blue", "orange", "green", "red", "purple", "brown", "pink", "gray", "olive", "cyan", "black"}
LINE_STYLES = {"solid": "-", "dashed": "--", "dotted": ":", "dashdot": "-."}
MARKERS = {"none": "", "circle": "o", "square": "s", "triangle": "^", "cross": "x", "plus": "+"}
MIME_TYPES = {"png": "image/png", "pdf": "application/pdf", "svg": "image/svg+xml"}


def _range(value: Any, label: str) -> tuple[float, float]:
    items = list(value or [])
    if len(items) != 2:
        raise ValueError(f"{label}必须包含两个有限数。")
    lower, upper = float(items[0]), float(items[1])
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise ValueError(f"{label}下限必须小于上限，且两者都必须有限。")
    if abs(lower) > 1e9 or abs(upper) > 1e9:
        raise ValueError(f"{label}绝对值不能超过 1e9。")
    return lower, upper


def _label(value: Any, limit: int = 120) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()[:limit]


def _style(raw: Any, index: int) -> dict[str, Any]:
    item = dict(raw or {}) if isinstance(raw, dict) else {}
    default_colors = ("blue", "orange", "green", "red", "purple", "brown")
    color = str(item.get("color") or default_colors[index % len(default_colors)]).casefold()
    line_style = str(item.get("line_style") or "solid").casefold()
    marker = str(item.get("marker") or "none").casefold()
    if color not in ALLOWED_COLORS or line_style not in LINE_STYLES or marker not in MARKERS:
        raise ValueError("颜色、线型或标记类型不在允许列表中。")
    return {
        "color": color,
        "linestyle": LINE_STYLES[line_style],
        "marker": MARKERS[marker],
        "linewidth": max(0.6, min(float(item.get("line_width") or 1.8), 4.0)),
    }


def _numeric_values(expression: str, variable: str, samples: np.ndarray) -> tuple[np.ndarray, dict[str, int]]:
    safe = _safe_expression(expression)
    locals_map = _local_dict(safe, variable)
    symbol = locals_map.get(variable)
    if symbol is None:
        import sympy as sp

        symbol = sp.Symbol(variable, real=True)
        locals_map[variable] = symbol
    parsed = _parse(safe, locals_map)
    import sympy as sp

    function = sp.lambdify(symbol, parsed, modules=["numpy"])
    with np.errstate(all="ignore"):
        raw = function(samples)
    array = np.asarray(raw)
    if array.ndim == 0:
        array = np.full(samples.shape, array.item())
    try:
        array = np.broadcast_to(array, samples.shape).astype(complex)
    except (TypeError, ValueError):
        raise ValueError("表达式没有生成与采样变量匹配的数值结果。") from None
    complex_mask = np.abs(array.imag) > 1e-10
    values = array.real.astype(float)
    nonfinite_mask = ~np.isfinite(values)
    invalid = complex_mask | nonfinite_mask
    values[invalid] = np.nan
    return values, {
        "complex_sample_count": int(np.count_nonzero(complex_mask)),
        "invalid_sample_count": int(np.count_nonzero(nonfinite_mask)),
    }


def _break_jumps(values: np.ndarray) -> tuple[np.ndarray, int]:
    result = values.copy()
    finite = np.isfinite(result)
    finite_values = result[finite]
    if finite_values.size < 3:
        return result, 0
    differences = np.abs(np.diff(result))
    valid_differences = differences[np.isfinite(differences)]
    median_difference = float(np.median(valid_differences)) if valid_differences.size else 0.0
    low, high = np.percentile(finite_values, [5, 95])
    robust_span = max(float(high - low), 1e-12)
    threshold = max(25.0 * median_difference, 1.5 * robust_span)
    jump_mask = np.isfinite(differences) & (differences > threshold)
    indices = np.flatnonzero(jump_mask) + 1
    result[indices] = np.nan
    return result, int(indices.size)


def _artifact(path: Path, format_name: str) -> dict[str, Any]:
    return {
        "artifact_id": path.stem,
        "kind": "math_plot",
        "format": format_name,
        "absolute_path": str(path.resolve()),
        "relative_path": path.relative_to(ROOT_DIR).as_posix(),
        "mime_type": MIME_TYPES[format_name],
        "size_bytes": path.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def plot_math_function(
    *,
    artifact_dir: str,
    plot_type: str,
    expressions: list[str] | None = None,
    variable: str = "x",
    parameter: str = "t",
    x_expression: str = "",
    y_expression: str = "",
    points: list[list[float]] | None = None,
    x_range: list[float] | None = None,
    y_range: list[float] | None = None,
    parameter_range: list[float] | None = None,
    sample_count: int = 1200,
    title: str = "",
    x_label: str = "x",
    y_label: str = "y",
    show_grid: bool = True,
    show_legend: bool = True,
    axis_mode: str = "standard",
    styles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output = Path(artifact_dir).resolve()
    if output.parent != ARTIFACT_ROOT or not output.name.startswith("run_"):
        raise ValueError("绘图产物目录不在受控缓存范围内。")
    output.mkdir(parents=True, exist_ok=True)
    plot_type = str(plot_type or "").casefold()
    if plot_type not in {"explicit_2d", "parametric_2d", "points_2d"}:
        raise ValueError("第一版只支持 explicit_2d、parametric_2d 和 points_2d。")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ValueError("显函数变量名无效。")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parameter):
        raise ValueError("参数曲线变量名无效。")
    sample_count = max(100, min(int(sample_count), 5000))
    if int(sample_count) != int(sample_count):
        raise ValueError("采样点数无效。")
    if axis_mode not in {"standard", "equal", "origin"}:
        raise ValueError("axis_mode 只能是 standard、equal 或 origin。")

    warnings: list[str] = []
    invalid_total = 0
    complex_total = 0
    break_total = 0
    figure, axis = plt.subplots(figsize=(8.0, 5.2), dpi=120)
    try:
        if plot_type == "explicit_2d":
            items = [str(item) for item in (expressions or [])]
            if not items or len(items) > 6:
                raise ValueError("显函数绘图必须提供一到六个表达式。")
            lower, upper = _range(x_range or [-10, 10], "x_range")
            samples = np.linspace(lower, upper, sample_count)
            style_items = list(styles or [])
            for index, expression in enumerate(items):
                values, counts = _numeric_values(expression, variable, samples)
                values, breaks = _break_jumps(values)
                invalid_total += counts["invalid_sample_count"]
                complex_total += counts["complex_sample_count"]
                break_total += breaks
                if not np.any(np.isfinite(values)):
                    warnings.append(f"表达式 {expression} 在当前范围内没有可绘制的实数采样点。")
                    continue
                axis.plot(samples, values, label=expression, **_style(style_items[index] if index < len(style_items) else {}, index))
        elif plot_type == "parametric_2d":
            if not x_expression or not y_expression:
                raise ValueError("参数曲线必须同时提供 x_expression 和 y_expression。")
            lower, upper = _range(parameter_range or [0, 2 * math.pi], "parameter_range")
            samples = np.linspace(lower, upper, sample_count)
            x_values, x_counts = _numeric_values(x_expression, parameter, samples)
            y_values, y_counts = _numeric_values(y_expression, parameter, samples)
            invalid_total += x_counts["invalid_sample_count"] + y_counts["invalid_sample_count"]
            complex_total += x_counts["complex_sample_count"] + y_counts["complex_sample_count"]
            invalid = ~np.isfinite(x_values) | ~np.isfinite(y_values)
            x_values[invalid] = np.nan
            y_values[invalid] = np.nan
            distances = np.sqrt(np.diff(x_values) ** 2 + np.diff(y_values) ** 2)
            finite_distances = distances[np.isfinite(distances)]
            if finite_distances.size:
                threshold = max(25 * float(np.median(finite_distances)), 1e-12)
                indices = np.flatnonzero(np.isfinite(distances) & (distances > threshold)) + 1
                x_values[indices] = np.nan
                y_values[indices] = np.nan
                break_total += int(indices.size)
            if not np.any(np.isfinite(x_values) & np.isfinite(y_values)):
                raise ValueError("参数曲线在当前范围内没有可绘制的实数采样点。")
            axis.plot(x_values, y_values, label=f"({x_expression}, {y_expression})", **_style((styles or [{}])[0], 0))
        else:
            raw_points = list(points or [])
            if not raw_points or len(raw_points) > 10000:
                raise ValueError("点列必须包含一到一万个二维点。")
            try:
                coordinates = np.asarray([[float(item[0]), float(item[1])] for item in raw_points if len(item) == 2], dtype=float)
            except (TypeError, ValueError, IndexError):
                raise ValueError("每个点必须由两个有限数构成。") from None
            if coordinates.shape != (len(raw_points), 2) or not np.all(np.isfinite(coordinates)):
                raise ValueError("散点中包含非法或非有限坐标。")
            style = _style((styles or [{}])[0], 0)
            axis.scatter(coordinates[:, 0], coordinates[:, 1], color=style["color"], marker=style["marker"] or "o", label="points")

        if y_range is not None:
            axis.set_ylim(*_range(y_range, "y_range"))
        if plot_type == "points_2d" and x_range is not None:
            axis.set_xlim(*_range(x_range, "x_range"))
        axis.set_title(_label(title))
        axis.set_xlabel(_label(x_label) or "x")
        axis.set_ylabel(_label(y_label) or "y")
        axis.grid(bool(show_grid), alpha=0.28)
        if axis_mode == "equal":
            axis.set_aspect("equal", adjustable="datalim")
        elif axis_mode == "origin":
            axis.spines["left"].set_position("zero")
            axis.spines["bottom"].set_position("zero")
            axis.spines["right"].set_color("none")
            axis.spines["top"].set_color("none")
        handles, labels = axis.get_legend_handles_labels()
        if show_legend and handles:
            axis.legend()
        if not axis.lines and not axis.collections:
            raise ValueError("全部采样点均无效，没有生成图形。")
        figure.tight_layout()
        paths = {
            "png": output / "preview.png",
            "pdf": output / "figure.pdf",
            "svg": output / "figure.svg",
        }
        figure.savefig(paths["png"], dpi=180, bbox_inches="tight")
        figure.savefig(paths["pdf"], bbox_inches="tight")
        figure.savefig(paths["svg"], bbox_inches="tight")
    finally:
        plt.close(figure)

    if invalid_total:
        warnings.append(f"已屏蔽 {invalid_total} 个非有限采样值。")
    if complex_total:
        warnings.append(f"已屏蔽 {complex_total} 个非实数采样值。")
    if break_total:
        warnings.append(f"已在 {break_total} 个疑似间断或异常跳跃处断开曲线。")
    warnings.append("图形采用有限范围均匀采样，仅用于数值或几何辅助，不能代替严格证明。")
    metadata = {
        "plot_type": plot_type,
        "expressions": list(expressions or []),
        "variable": variable,
        "parameter": parameter,
        "x_expression": x_expression,
        "y_expression": y_expression,
        "x_range": list(x_range or []),
        "y_range": list(y_range or []) if y_range is not None else None,
        "parameter_range": list(parameter_range or []),
        "sample_count": sample_count,
        "invalid_sample_count": invalid_total,
        "complex_sample_count": complex_total,
        "discontinuity_break_count": break_total,
        "sampling_method": "uniform_with_nonfinite_and_jump_breaks",
        "warnings": warnings,
        "matplotlib_backend": matplotlib.get_backend(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path = output / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artifacts = [_artifact(paths[name], name) for name in ("png", "pdf", "svg")]
    artifacts.append(
        {
            "artifact_id": metadata_path.stem,
            "kind": "plot_metadata",
            "format": "json",
            "absolute_path": str(metadata_path.resolve()),
            "relative_path": metadata_path.relative_to(ROOT_DIR).as_posix(),
            "mime_type": "application/json",
            "size_bytes": metadata_path.stat().st_size,
            "created_at": metadata["created_at"],
        }
    )
    return {
        "operation": "plot_math_function",
        "result": "已生成受控 Python 二维静态图形。",
        "result_latex": "已生成受控 Python 二维静态图形。",
        "verification": "matplotlib_static_plot",
        "png_path": str(paths["png"].resolve()),
        "pdf_path": str(paths["pdf"].resolve()),
        "svg_path": str(paths["svg"].resolve()),
        "metadata_path": str(metadata_path.resolve()),
        "artifacts": artifacts,
        "warnings": warnings,
        "plot_metadata": metadata,
    }
