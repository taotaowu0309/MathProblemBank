from __future__ import annotations

import asyncio
import importlib.metadata
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*\Z")
_SAFE_EXPR = re.compile(r"[0-9A-Za-z\s+\-*/^().,={}\[\]<>!|&']+\Z")
_DOMAINS = {"unspecified": "", "real": "Reals", "complex": "Complexes", "integer": "Integers"}
ROOT_DIR = APP_PATHS.application_root
ARTIFACT_ROOT = (APP_PATHS.cache_dir / "ai_agent_artifacts").resolve()
PLOT_TYPES = {
    "explicit_2d",
    "parametric_2d",
    "implicit_2d",
    "region_2d",
    "surface_3d",
    "parametric_curve_3d",
    "parametric_surface_3d",
    "implicit_3d",
    "vector_field_2d",
}


def _safe_expression(value: Any, *, label: str = "表达式") -> str:
    text = str(value or "").strip()
    if not text or len(text) > 4000:
        raise ValueError(f"{label}为空或过长。")
    if ";" in text or "`" in text or '"' in text or not _SAFE_EXPR.fullmatch(text):
        raise ValueError(f"{label}包含不允许的 Wolfram Language 语法。")
    return text


def _variables(raw: Any) -> list[str]:
    values: list[str] = []
    for item in raw or []:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if not _NAME.fullmatch(name):
            raise ValueError("变量名必须由英文字母和数字组成。")
        values.append(name)
    return values


def _variable(value: Any, *, label: str, default: str) -> str:
    name = str(value or default).strip()
    if not _NAME.fullmatch(name):
        raise ValueError(f"{label}必须由英文字母和数字组成，且以字母开头。")
    return name


def _finite_range(value: Any, *, label: str, default: tuple[float, float]) -> tuple[str, str]:
    items = list(value if value not in (None, "") else default)
    if len(items) != 2:
        raise ValueError(f"{label}必须包含两个有限数。")
    lower, upper = float(items[0]), float(items[1])
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise ValueError(f"{label}下限必须小于上限，且两者都必须有限。")
    if abs(lower) > 1e9 or abs(upper) > 1e9:
        raise ValueError(f"{label}绝对值不能超过 1e9。")
    return format(lower, ".15g"), format(upper, ".15g")


def _expression_list(raw: Any, *, label: str, maximum: int = 6) -> list[str]:
    values = [_safe_expression(item, label=label) for item in list(raw or [])]
    if not values or len(values) > maximum:
        raise ValueError(f"{label}必须包含一到 {maximum} 个表达式。")
    return values


def _short_text(value: Any, *, limit: int = 160) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()[:limit]


def _wl_string(value: Any, *, limit: int = 160) -> str:
    return json.dumps(_short_text(value, limit=limit), ensure_ascii=False)


def _plot_options(
    arguments: dict[str, Any],
    *,
    labels: list[str],
    frame_labels: bool = False,
    supports_grid: bool = False,
    supports_legend: bool = False,
    supports_mesh: bool = False,
) -> list[str]:
    image_size = max(320, min(int(arguments.get("image_size") or 720), 1600))
    options = [f"ImageSize -> {image_size}", "ImagePadding -> 30", "ImageMargins -> 30", "PlotRange -> All"]
    title = _short_text(arguments.get("title"))
    if title:
        options.append(f"PlotLabel -> {_wl_string(title)}")
    cleaned_labels = [_short_text(item, limit=80) for item in labels]
    if any(cleaned_labels):
        head = "FrameLabel" if frame_labels else "AxesLabel"
        options.append(f"{head} -> {{{','.join(_wl_string(item, limit=80) for item in cleaned_labels)}}}")
    if frame_labels:
        options.append(f"Frame -> {'True' if bool(arguments.get('show_axes', True)) else 'False'}")
    else:
        options.append(f"Axes -> {'True' if bool(arguments.get('show_axes', True)) else 'False'}")
    if supports_grid:
        options.append(f"GridLines -> {'Automatic' if bool(arguments.get('show_grid', True)) else 'None'}")
    if supports_legend and bool(arguments.get("show_legend", True)):
        legend_labels = [_short_text(item, limit=80) for item in list(arguments.get("legend_labels") or [])]
        if legend_labels:
            options.append("PlotLegends -> {" + ",".join(_wl_string(item, limit=80) for item in legend_labels[:6]) + "}")
        else:
            options.append("PlotLegends -> Automatic")
    if supports_mesh:
        mesh = str(arguments.get("mesh") or "automatic").casefold()
        if mesh not in {"automatic", "none", "all"}:
            raise ValueError("mesh 只能是 automatic、none 或 all。")
        options.append({"automatic": "Mesh -> Automatic", "none": "Mesh -> None", "all": "Mesh -> All"}[mesh])
    theme = str(arguments.get("theme") or "default").casefold()
    themes = {"default": "", "scientific": 'PlotTheme -> "Scientific"', "classic": 'PlotTheme -> "Classic"', "monochrome": 'PlotTheme -> "Monochrome"'}
    if theme not in themes:
        raise ValueError("theme 只能是 default、scientific、classic 或 monochrome。")
    if themes[theme]:
        options.append(themes[theme])
    return options


def _build_plot_expression(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    plot_type = str(arguments.get("plot_type") or "").casefold()
    if plot_type not in PLOT_TYPES:
        raise ValueError(f"不支持的 Mathematica plot_type：{plot_type}")
    x = _variable(arguments.get("variable"), label="第一个变量", default="x")
    y = _variable(arguments.get("second_variable"), label="第二个变量", default="y")
    z = _variable(arguments.get("third_variable"), label="第三个变量", default="z")
    t = _variable(arguments.get("parameter"), label="参数变量", default="t")
    u = _variable(arguments.get("second_parameter"), label="第二参数变量", default="u")
    xr = _finite_range(arguments.get("x_range"), label="x_range", default=(-10.0, 10.0))
    yr = _finite_range(arguments.get("y_range"), label="y_range", default=(-10.0, 10.0))
    zr = _finite_range(arguments.get("z_range"), label="z_range", default=(-10.0, 10.0))
    tr = _finite_range(arguments.get("parameter_range"), label="parameter_range", default=(0.0, 2.0 * math.pi))
    ur = _finite_range(arguments.get("second_parameter_range"), label="second_parameter_range", default=(0.0, 2.0 * math.pi))
    plot_points = max(20, min(int(arguments.get("plot_points") or 55), 400))
    max_recursion = max(0, min(int(arguments.get("max_recursion") or 3), 8))
    title = _short_text(arguments.get("title"))
    labels = {
        "x": _short_text(arguments.get("x_label") or x, limit=80),
        "y": _short_text(arguments.get("y_label") or y, limit=80),
        "z": _short_text(arguments.get("z_label") or z, limit=80),
    }

    if plot_type == "explicit_2d":
        expressions = _expression_list(arguments.get("expressions"), label="二维显函数")
        plot_arguments = dict(arguments)
        if len(expressions) > 1 and not plot_arguments.get("legend_labels"):
            plot_arguments["legend_labels"] = expressions
        options = _plot_options(plot_arguments, labels=[labels["x"], labels["y"]], supports_grid=True, supports_legend=len(expressions) > 1)
        options += [f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"Plot[{{{','.join(expressions)}}}, {{{x},{xr[0]},{xr[1]}}}, {','.join(options)}]"
    elif plot_type == "parametric_2d":
        x_expression = _safe_expression(arguments.get("x_expression"), label="参数曲线 x 表达式")
        y_expression = _safe_expression(arguments.get("y_expression"), label="参数曲线 y 表达式")
        expressions = [x_expression, y_expression]
        options = _plot_options(arguments, labels=[labels["x"], labels["y"]], supports_grid=True)
        options += [f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"ParametricPlot[{{{x_expression},{y_expression}}}, {{{t},{tr[0]},{tr[1]}}}, {','.join(options)}]"
    elif plot_type == "implicit_2d":
        expressions = _expression_list(arguments.get("expressions"), label="隐式曲线表达式")
        options = _plot_options(arguments, labels=[labels["x"], labels["y"]], frame_labels=True, supports_grid=True)
        options += ["Contours -> {0}", f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"ContourPlot[{{{','.join(expressions)}}}, {{{x},{xr[0]},{xr[1]}}}, {{{y},{yr[0]},{yr[1]}}}, {','.join(options)}]"
    elif plot_type == "region_2d":
        relation = _safe_expression(arguments.get("expression"), label="区域关系")
        expressions = [relation]
        options = _plot_options(arguments, labels=[labels["x"], labels["y"]], frame_labels=True, supports_grid=True)
        options += [f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"RegionPlot[{relation}, {{{x},{xr[0]},{xr[1]}}}, {{{y},{yr[0]},{yr[1]}}}, {','.join(options)}]"
    elif plot_type == "surface_3d":
        expressions = _expression_list(arguments.get("expressions"), label="三维曲面表达式", maximum=4)
        plot_arguments = dict(arguments)
        if len(expressions) > 1 and not plot_arguments.get("legend_labels"):
            plot_arguments["legend_labels"] = expressions
        options = _plot_options(plot_arguments, labels=[labels["x"], labels["y"], labels["z"]], supports_legend=len(expressions) > 1, supports_mesh=True)
        options += [f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"Plot3D[{{{','.join(expressions)}}}, {{{x},{xr[0]},{xr[1]}}}, {{{y},{yr[0]},{yr[1]}}}, {','.join(options)}]"
    elif plot_type == "parametric_curve_3d":
        x_expression = _safe_expression(arguments.get("x_expression"), label="空间曲线 x 表达式")
        y_expression = _safe_expression(arguments.get("y_expression"), label="空间曲线 y 表达式")
        z_expression = _safe_expression(arguments.get("z_expression"), label="空间曲线 z 表达式")
        expressions = [x_expression, y_expression, z_expression]
        options = _plot_options(arguments, labels=[labels["x"], labels["y"], labels["z"]])
        options += [f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"ParametricPlot3D[{{{x_expression},{y_expression},{z_expression}}}, {{{t},{tr[0]},{tr[1]}}}, {','.join(options)}]"
    elif plot_type == "parametric_surface_3d":
        x_expression = _safe_expression(arguments.get("x_expression"), label="参数曲面 x 表达式")
        y_expression = _safe_expression(arguments.get("y_expression"), label="参数曲面 y 表达式")
        z_expression = _safe_expression(arguments.get("z_expression"), label="参数曲面 z 表达式")
        expressions = [x_expression, y_expression, z_expression]
        options = _plot_options(arguments, labels=[labels["x"], labels["y"], labels["z"]], supports_mesh=True)
        options += [f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"ParametricPlot3D[{{{x_expression},{y_expression},{z_expression}}}, {{{t},{tr[0]},{tr[1]}}}, {{{u},{ur[0]},{ur[1]}}}, {','.join(options)}]"
    elif plot_type == "implicit_3d":
        implicit = _safe_expression(arguments.get("expression"), label="隐式曲面表达式")
        expressions = [implicit]
        options = _plot_options(arguments, labels=[labels["x"], labels["y"], labels["z"]], supports_mesh=True)
        options += ["Contours -> {0}", f"PlotPoints -> {plot_points}", f"MaxRecursion -> {max_recursion}"]
        expression = f"ContourPlot3D[{implicit}, {{{x},{xr[0]},{xr[1]}}}, {{{y},{yr[0]},{yr[1]}}}, {{{z},{zr[0]},{zr[1]}}}, {','.join(options)}]"
    else:
        first = _safe_expression(arguments.get("u_expression"), label="向量场第一分量")
        second = _safe_expression(arguments.get("v_expression"), label="向量场第二分量")
        expressions = [first, second]
        options = _plot_options(arguments, labels=[labels["x"], labels["y"]], frame_labels=True)
        vector_points = max(8, min(plot_points, 50))
        options.append(f"VectorPoints -> {vector_points}")
        expression = f"VectorPlot[{{{first},{second}}}, {{{x},{xr[0]},{xr[1]}}}, {{{y},{yr[0]},{yr[1]}}}, {','.join(options)}]"

    return expression, {
        "plot_type": plot_type,
        "expressions": expressions,
        "variables": {"x": x, "y": y, "z": z, "parameter": t, "second_parameter": u},
        "ranges": {"x": list(xr), "y": list(yr), "z": list(zr), "parameter": list(tr), "second_parameter": list(ur)},
        "plot_points": plot_points,
        "max_recursion": max_recursion,
        "image_size": max(320, min(int(arguments.get("image_size") or 720), 1600)),
        "title": title,
    }


def _artifact_directory(value: Any) -> Path:
    output = Path(str(value or "")).resolve()
    if output.parent != ARTIFACT_ROOT or not output.name.startswith("run_"):
        raise ValueError("Mathematica 绘图产物目录不在受控缓存范围内。")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _assumptions(raw: Any, domain: str, variables: list[str]) -> str:
    parts: list[str] = []
    domain_name = _DOMAINS.get(domain)
    if domain_name is None:
        raise ValueError("domain 只能是 unspecified、real、complex 或 integer。")
    if domain_name and variables:
        parts.append(f"Element[{{{','.join(variables)}}}, {domain_name}]")
    properties = {
        "positive": "# > 0",
        "negative": "# < 0",
        "nonzero": "# != 0",
        "real": "Element[#, Reals]",
        "integer": "Element[#, Integers]",
    }
    relations = {"=": "==", "==": "==", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
    for item in raw or []:
        if not isinstance(item, dict):
            raise ValueError("assumptions 中的每一项必须是对象。")
        if item.get("property"):
            name = str(item.get("variable") or "")
            prop = str(item.get("property") or "")
            if not _NAME.fullmatch(name) or prop not in properties:
                raise ValueError("假设中的变量或 property 不受支持。")
            parts.append(properties[prop].replace("#", name))
        else:
            left = _safe_expression(item.get("left"), label="假设左侧")
            right = _safe_expression(item.get("right"), label="假设右侧")
            relation = relations.get(str(item.get("relation") or ""))
            if not relation:
                raise ValueError("假设关系不受支持。")
            parts.append(f"({left}) {relation} ({right})")
    return " && ".join(parts) if parts else "True"


def _build_expression(arguments: dict[str, Any]) -> str:
    operation = str(arguments.get("operation") or "").casefold()
    raw_expression = arguments.get("expression")
    if raw_expression in (None, "") and isinstance(arguments.get("matrix"), list):
        raw_expression = "{" + ",".join(
            "{" + ",".join(str(value) for value in row) + "}"
            for row in arguments["matrix"]
            if isinstance(row, list)
        ) + "}"
    expression = _safe_expression(raw_expression)
    raw_variables = arguments.get("variables") or ([arguments.get("variable")] if arguments.get("variable") else [])
    variables = _variables(raw_variables)
    parameters = dict(arguments.get("parameters") or {})
    domain = str(arguments.get("domain") or "unspecified").casefold()
    if domain == "unspecified":
        specified_domains = {
            str(item.get("domain") or "unspecified").casefold()
            for item in raw_variables
            if isinstance(item, dict) and str(item.get("domain") or "unspecified").casefold() != "unspecified"
        }
        if len(specified_domains) == 1:
            domain = specified_domains.pop()
    assumptions = _assumptions(arguments.get("assumptions"), domain, variables)
    variable = str(parameters.get("variable") or (variables[0] if variables else "x"))
    if not _NAME.fullmatch(variable):
        raise ValueError("计算变量名无效。")
    variable_list = "{" + ",".join(variables or [variable]) + "}"
    domain_arg = _DOMAINS.get(domain) or "Complexes"
    if operation == "simplify":
        body = f"FullSimplify[{expression}, Assumptions -> {assumptions}]"
    elif operation == "factor":
        body = f"Factor[{expression}]"
    elif operation in {"solve", "reduce"}:
        head = "Solve" if operation == "solve" else "Reduce"
        body = f"{head}[{expression}, {variable_list}, {domain_arg}]"
    elif operation == "integrate":
        lower = parameters.get("lower")
        upper = parameters.get("upper")
        iterator = f"{{{variable},{_safe_expression(lower, label='积分下限')},{_safe_expression(upper, label='积分上限')}}}" if lower not in (None, "") and upper not in (None, "") else variable
        body = f"Assuming[{assumptions}, Integrate[{expression}, {iterator}]]"
    elif operation == "limit":
        point = _safe_expression(parameters.get("point"), label="极限点")
        body = f"Assuming[{assumptions}, Limit[{expression}, {variable} -> {point}]]"
    elif operation == "sum":
        lower = _safe_expression(parameters.get("lower"), label="求和下限")
        upper = _safe_expression(parameters.get("upper"), label="求和上限")
        body = f"Assuming[{assumptions}, Sum[{expression}, {{{variable},{lower},{upper}}}]]"
    elif operation == "series":
        point = _safe_expression(parameters.get("point", "0"), label="展开点")
        order = max(1, min(int(parameters.get("order") or 6), 30))
        body = f"Assuming[{assumptions}, Series[{expression}, {{{variable},{point},{order}}}]]"
    elif operation == "dsolve":
        dependent = str(parameters.get("dependent") or "y")
        if not _NAME.fullmatch(dependent):
            raise ValueError("因变量名无效。")
        body = f"DSolve[{expression}, {dependent}, {variable}]"
    elif operation == "determinant":
        body = f"Det[{expression}]"
    elif operation == "eigenvalues":
        body = f"Eigenvalues[{expression}]"
    elif operation == "numeric":
        precision = max(15, min(int(arguments.get("precision") or 30), 100))
        body = f"N[{expression}, {precision}]"
    else:
        raise ValueError(f"不支持的 Mathematica operation：{operation}")
    return body


async def _compute(app: Any, evaluate: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    expression = _build_expression(arguments)
    raw = await evaluate(app.ctx, expression, "InputForm")
    formatted = await evaluate(app.ctx, expression, "TeXForm")
    conditions = []
    for marker in ("ConditionalExpression", "Piecewise", "GeneratedParameters"):
        if marker in raw:
            conditions.append(raw)
            break
    return {
        "success": True,
        "raw_output": raw,
        "formatted_output": formatted,
        "numeric_approximation": raw if str(arguments.get("operation") or "") == "numeric" else "",
        "conditions": conditions,
        "mma_mcp_version": importlib.metadata.version("mma-mcp"),
    }


async def _plot(
    app: Any,
    evaluate_image: Any,
    arguments: dict[str, Any],
    artifact_dir: Any,
) -> dict[str, Any]:
    expression, metadata = _build_plot_expression(arguments)
    output = _artifact_directory(artifact_dir)
    png_path = output / "preview.png"
    pdf_path = output / "figure.pdf"
    svg_path = output / "figure.svg"
    source_path = output / "generated_plot.wl"
    metadata_path = output / "metadata.json"
    source_path.write_text(expression + "\n", encoding="utf-8")

    image = await evaluate_image(app.ctx, expression)
    png_data = bytes(image.data)
    if not png_data.startswith(bytes.fromhex("89504e470d0a1a0a")):
        raise RuntimeError("mma-mcp evaluate_image 没有返回有效 PNG 数据。")
    png_path.write_bytes(png_data)

    warnings: list[str] = []

    def export_vectors() -> None:
        from mma_mcp.kernel import _eval_in_context
        from wolframclient.language import wl, wlexpr

        app.ctx.check(expression)
        with app.ctx.pool.worker() as (kernel, wl_context):
            inner = _eval_in_context(expression, wl_context, app.ctx.timeout)
            for path, format_name in ((pdf_path, "PDF"), (svg_path, "SVG")):
                try:
                    kernel.evaluate(
                        wl.Export(str(path), wlexpr(inner), format_name),
                        hard_timeout=app.ctx.hard_timeout,
                    )
                    if not path.is_file() or path.stat().st_size <= 0:
                        raise RuntimeError(f"Mathematica 没有生成有效 {format_name} 文件。")
                except Exception as error:
                    path.unlink(missing_ok=True)
                    warnings.append(f"{format_name} 导出失败：{type(error).__name__}: {error}"[:900])

    await asyncio.get_running_loop().run_in_executor(None, export_vectors)
    metadata.update(
        {
            "engine": "mathematica",
            "generated_expression": expression,
            "mma_mcp_version": importlib.metadata.version("mma-mcp"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": warnings,
            "exported_formats": [
                format_name
                for format_name, path in (("png", png_path), ("pdf", pdf_path), ("svg", svg_path))
                if path.is_file() and path.stat().st_size > 0
            ],
        }
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "success": True,
        "raw_output": "Mathematica 图形已生成。",
        "formatted_output": "Mathematica 图形已生成。",
        "png_path": str(png_path),
        "pdf_path": str(pdf_path) if pdf_path.is_file() else "",
        "svg_path": str(svg_path) if svg_path.is_file() else "",
        "source_path": str(source_path),
        "metadata_path": str(metadata_path),
        "plot_metadata": metadata,
        "warnings": warnings,
        "mma_mcp_version": importlib.metadata.version("mma-mcp"),
    }


def main() -> int:
    try:
        from mma_mcp.server import App
        from mma_mcp.tools.evaluate import evaluate, evaluate_image
    except Exception as error:
        sys.stdout.write(json.dumps({"id": "startup", "success": False, "error": f"mma-mcp 接口不可用：{error}"}) + "\n")
        sys.stdout.flush()
        return 2
    app = App()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("桥接请求必须是 JSON 对象。")
                if request.get("command") == "shutdown":
                    break
                request_id = str(request.get("id") or "")
                command = str(request.get("command") or "compute")
                if command == "compute":
                    response = loop.run_until_complete(_compute(app, evaluate, dict(request.get("arguments") or {})))
                elif command == "plot":
                    response = loop.run_until_complete(
                        _plot(
                            app,
                            evaluate_image,
                            dict(request.get("arguments") or {}),
                            request.get("artifact_dir"),
                        )
                    )
                else:
                    raise ValueError(f"不支持的 Mathematica 桥接命令：{command}")
                response["id"] = request_id
            except Exception as error:
                response = {
                    "id": str(request.get("id") or "") if isinstance(locals().get("request"), dict) else "",
                    "success": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    finally:
        try:
            app.pool.stop()
        except Exception:
            pass
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
