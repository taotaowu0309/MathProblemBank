from __future__ import annotations

import re
from itertools import product
from math import isfinite
from typing import Any

import sympy as sp
import numpy as np
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)


TRANSFORMATIONS = standard_transformations + (convert_xor, implicit_multiplication_application)
ALLOWED_FUNCTIONS: dict[str, Any] = {
    "sin": sp.sin,
    "cos": sp.cos,
    "tan": sp.tan,
    "asin": sp.asin,
    "acos": sp.acos,
    "atan": sp.atan,
    "sinh": sp.sinh,
    "cosh": sp.cosh,
    "tanh": sp.tanh,
    "exp": sp.exp,
    "log": sp.log,
    "sqrt": sp.sqrt,
    "Abs": sp.Abs,
    "floor": sp.floor,
    "ceiling": sp.ceiling,
    "gamma": sp.gamma,
    "factorial": sp.factorial,
    "Min": sp.Min,
    "Max": sp.Max,
}
ALLOWED_CONSTANTS = {"pi": sp.pi, "E": sp.E, "I": sp.I, "oo": sp.oo}
GLOBAL_DICT = {
    "__builtins__": {},
    "Symbol": sp.Symbol,
    "Integer": sp.Integer,
    "Float": sp.Float,
    "Rational": sp.Rational,
    **ALLOWED_FUNCTIONS,
    **ALLOWED_CONSTANTS,
}


def _safe_expression(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        raise ValueError("数学表达式不能为空。")
    if len(value) > 800:
        raise ValueError("数学表达式过长；请拆成较小的计算。")
    if value.count("(") != value.count(")"):
        raise ValueError("数学表达式的括号不匹配。")
    if re.search(r"__|[\[\]{};'\"`:\\]|[A-Za-z_]\s*\.|\.\s*[A-Za-z_]", value):
        raise ValueError("表达式包含属性访问、字符串、容器或其他不允许的语法。")
    if not re.fullmatch(r"[0-9A-Za-z_à-öø-ÿ\s+\-*/^().,=<>]+", value):
        raise ValueError("表达式包含不受支持的字符。")
    if sum(value.count(operator) for operator in ("+", "-", "*", "/", "^")) > 160:
        raise ValueError("表达式运算过于复杂；请拆分后再计算。")
    return value


def _local_dict(
    *expressions: str,
    symbol_properties: dict[str, dict[str, bool]] | None = None,
) -> dict[str, Any]:
    names: set[str] = set()
    for expression in expressions:
        names.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expression))
    unknown_calls = [
        name
        for expression in expressions
        for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression)
        if name not in ALLOWED_FUNCTIONS
    ]
    if unknown_calls:
        raise ValueError("不允许调用未知函数：" + "、".join(sorted(set(unknown_calls))))
    return {
        name: sp.Symbol(name, **dict((symbol_properties or {}).get(name) or {}))
        for name in names
        if name not in ALLOWED_FUNCTIONS and name not in ALLOWED_CONSTANTS
    }


def _symbol_properties(
    variables: list[dict[str, Any] | str] | None,
    domain: str,
    assumptions: list[dict[str, Any]] | None,
) -> dict[str, dict[str, bool]]:
    result: dict[str, dict[str, bool]] = {}
    domain_map = {
        "real": {"real": True},
        "integer": {"integer": True},
        "complex": {"complex": True},
    }
    for item in variables or []:
        name = str(item.get("name") if isinstance(item, dict) else item or "")
        item_domain = str((item.get("domain") if isinstance(item, dict) else domain) or domain or "unspecified")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            result.setdefault(name, {}).update(domain_map.get(item_domain, {}))
    property_map = {
        "positive": {"positive": True},
        "negative": {"negative": True},
        "nonzero": {"nonzero": True},
        "real": {"real": True},
        "integer": {"integer": True},
    }
    for item in assumptions or []:
        if not isinstance(item, dict) or not item.get("property"):
            continue
        name = str(item.get("variable") or "")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            result.setdefault(name, {}).update(property_map.get(str(item.get("property") or ""), {}))
    return result


def _assumption_predicate(
    assumptions: list[dict[str, Any]] | None,
    local_dict: dict[str, Any],
) -> sp.Expr | bool:
    predicates: list[sp.Expr] = []
    relations = {
        "=": sp.Eq,
        "==": sp.Eq,
        "!=": sp.Ne,
        ">": sp.Gt,
        ">=": sp.Ge,
        "<": sp.Lt,
        "<=": sp.Le,
    }
    for item in assumptions or []:
        if not isinstance(item, dict) or item.get("property"):
            continue
        relation = relations.get(str(item.get("relation") or ""))
        if relation is None:
            continue
        predicates.append(
            relation(
                _parse(str(item.get("left") if item.get("left") is not None else ""), local_dict),
                _parse(str(item.get("right") if item.get("right") is not None else ""), local_dict),
            )
        )
    return sp.And(*predicates) if predicates else True


def _parse(text: str, local_dict: dict[str, Any]) -> sp.Expr:
    value = _safe_expression(text)
    if "=" in value:
        raise ValueError("该操作需要单个表达式；方程只在 solve 操作中使用。")
    try:
        parsed = parse_expr(
            value,
            local_dict={**ALLOWED_FUNCTIONS, **ALLOWED_CONSTANTS, **local_dict},
            global_dict=GLOBAL_DICT,
            transformations=TRANSFORMATIONS,
            evaluate=True,
        )
    except Exception as error:
        raise ValueError(f"无法解析数学表达式：{error}") from None
    if not isinstance(parsed, sp.Expr):
        raise ValueError("解析结果不是受支持的 SymPy 表达式。")
    return parsed


def _symbol(name: str, local_dict: dict[str, Any]) -> sp.Symbol:
    value = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("变量名必须是英文字母开头的符号。")
    symbol = local_dict.get(value) or sp.Symbol(value)
    if not isinstance(symbol, sp.Symbol):
        raise ValueError("所选变量不是符号。")
    return symbol


def _matrix(values: list[list[Any]] | None, *, numeric: bool = False, max_size: int = 30) -> Any:
    rows = list(values or [])
    if not rows or not all(isinstance(row, list) and row for row in rows):
        raise ValueError("矩阵必须是非空的二维数组。")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError("矩阵每一行的长度必须相同。")
    if len(rows) > max_size or width > max_size:
        raise ValueError(f"矩阵维数不能超过 {max_size}×{max_size}。")
    if numeric:
        try:
            return np.asarray([[float(value) for value in row] for row in rows], dtype=float)
        except (TypeError, ValueError):
            raise ValueError("数值矩阵只能包含有限实数。") from None
    expressions = [str(value) for row in rows for value in row]
    locals_map = _local_dict(*expressions)
    return sp.Matrix([[_parse(str(value), locals_map) for value in row] for row in rows])


def _matrix_payload(value: sp.MatrixBase) -> list[list[str]]:
    return [[str(value[row, column]) for column in range(value.cols)] for row in range(value.rows)]


def symbolic_math(
    operation: str,
    expression: str = "",
    *,
    variable: str = "x",
    second_expression: str = "",
    order: int = 1,
    lower: str = "",
    upper: str = "",
    point: str = "",
    direction: str = "+-",
    matrix: list[list[Any]] | None = None,
    second_matrix: list[list[Any]] | None = None,
    rhs: list[Any] | None = None,
    variables: list[dict[str, Any] | str] | None = None,
    domain: str = "unspecified",
    assumptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    operation = str(operation or "").strip().casefold()
    matrix_operations = {"determinant", "eigenvalues", "matrix_multiply", "matrix_inverse", "matrix_rank", "linear_solve"}
    first = "0" if operation in matrix_operations and not str(expression or "").strip() else _safe_expression(expression)
    second = _safe_expression(second_expression) if second_expression else ""
    bounds = [value for value in (lower, upper, point) if str(value).strip()]
    properties = _symbol_properties(variables, domain, assumptions)
    if variable not in properties and domain in {"real", "integer", "complex"}:
        properties[variable] = {domain: True}
    locals_map = _local_dict(first, second, *bounds, variable, symbol_properties=properties)
    symbol = _symbol(variable, locals_map)

    if operation in matrix_operations:
        first_matrix = _matrix(matrix)
        if operation == "determinant":
            if first_matrix.rows != first_matrix.cols:
                raise ValueError("行列式要求方阵。")
            result = first_matrix.det()
        elif operation == "eigenvalues":
            if first_matrix.rows != first_matrix.cols:
                raise ValueError("特征值要求方阵。")
            result = first_matrix.eigenvals()
        elif operation == "matrix_multiply":
            other = _matrix(second_matrix)
            if first_matrix.cols != other.rows:
                raise ValueError("两个矩阵的维数不能相乘。")
            result = first_matrix * other
        elif operation == "matrix_inverse":
            if first_matrix.rows != first_matrix.cols:
                raise ValueError("逆矩阵要求方阵。")
            result = first_matrix.inv()
        elif operation == "matrix_rank":
            result = first_matrix.rank()
        else:
            vector = list(rhs or [])
            if len(vector) != first_matrix.rows:
                raise ValueError("线性方程右端向量长度必须等于矩阵行数。")
            expressions = [str(value) for value in vector]
            locals_rhs = _local_dict(*expressions)
            rhs_matrix = sp.Matrix([_parse(value, locals_rhs) for value in expressions])
            result = sp.linsolve((first_matrix, rhs_matrix))
        payload: dict[str, Any] = {
            "operation": operation,
            "matrix": [[str(value) for value in row] for row in (matrix or [])],
            "result": str(result),
            "result_latex": sp.latex(result),
            "canonical_expression": str(result),
            "verification": "sympy_exact",
            "assumptions": list(assumptions or []),
            "domain": str(domain or "unspecified"),
        }
        if isinstance(result, sp.MatrixBase):
            payload["result_matrix"] = _matrix_payload(result)
        return payload

    if operation == "solve":
        if first.count("=") > 1:
            raise ValueError("一次只能求解一个方程。")
        if "=" in first:
            left, right = first.split("=", 1)
            equation = sp.Eq(_parse(left, locals_map), _parse(right, locals_map))
        else:
            equation = sp.Eq(_parse(first, locals_map), 0)
        result: Any = sp.solve(equation, symbol)
    else:
        parsed = _parse(first, locals_map)
        if operation == "simplify":
            result = sp.simplify(parsed)
        elif operation == "expand":
            result = sp.expand(parsed)
        elif operation == "factor":
            result = sp.factor(parsed)
        elif operation == "differentiate":
            order_value = max(1, min(int(order), 12))
            result = sp.diff(parsed, symbol, order_value)
        elif operation == "integrate":
            if bool(lower) != bool(upper):
                raise ValueError("定积分必须同时提供上下限。")
            if lower and upper:
                result = sp.integrate(
                    parsed,
                    (symbol, _parse(lower, locals_map), _parse(upper, locals_map)),
                )
            else:
                result = sp.integrate(parsed, symbol)
        elif operation == "limit":
            if not point:
                raise ValueError("极限运算必须提供趋近点。")
            if direction not in {"+", "-", "+-"}:
                raise ValueError("极限方向只能是 +、- 或 +-。")
            result = sp.limit(parsed, symbol, _parse(point, locals_map), dir=direction)
        elif operation == "equivalence":
            if not second:
                raise ValueError("等价性核验必须提供第二个表达式。")
            other = _parse(second, locals_map)
            difference = sp.simplify(parsed - other)
            equivalent = bool(difference == 0)
            return {
                "operation": operation,
                "expression": first,
                "second_expression": second,
                "equivalent": equivalent,
                "difference": str(difference),
                "difference_latex": sp.latex(difference),
                "verification": "symbolic_exact" if equivalent else "not_proved_equivalent",
            }
        else:
            raise ValueError(f"不支持的符号计算操作：{operation}")

    predicate = _assumption_predicate(assumptions, locals_map)
    if predicate is not True and isinstance(result, sp.Basic):
        result = sp.refine(result, predicate)
    return {
        "operation": operation,
        "expression": first,
        "variable": str(symbol),
        "result": str(result),
        "result_latex": sp.latex(result),
        "verification": "sympy_exact",
        "canonical_expression": str(result),
        "assumptions": list(assumptions or []),
        "domain": str(domain or "unspecified"),
        "conditions": [str(result)] if isinstance(result, sp.Piecewise) else [],
    }


def _parse_assignments(values: dict[str, Any] | None) -> tuple[dict[str, sp.Symbol], dict[sp.Symbol, sp.Expr]]:
    raw = dict(values or {})
    expressions = [_safe_expression(str(value)) for value in raw.values()]
    locals_map = _local_dict(*expressions, *[str(key) for key in raw])
    substitutions: dict[sp.Symbol, sp.Expr] = {}
    for name, value in raw.items():
        symbol = _symbol(str(name), locals_map)
        substitutions[symbol] = _parse(str(value), locals_map)
    return locals_map, substitutions


def numerical_math(
    operation: str,
    expression: str = "",
    *,
    values: dict[str, Any] | None = None,
    variable: str = "x",
    initial_guess: float = 0.0,
    order: int = 6,
    precision: int = 30,
    second_expression: str = "",
    lower: str = "",
    upper: str = "",
    matrix: list[list[Any]] | None = None,
    second_matrix: list[list[Any]] | None = None,
    rhs: list[Any] | None = None,
    variables: list[dict[str, Any] | str] | None = None,
    domain: str = "unspecified",
    assumptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Controlled high-precision numerical evaluation, root finding and series."""

    operation = str(operation or "").strip().casefold()
    precision = max(15, min(int(precision), 80))
    if operation in {
        "numerical_linear_solve",
        "numerical_eigenvalues",
        "numerical_matrix_multiply",
    }:
        first_matrix = _matrix(matrix, numeric=True, max_size=200)
        if operation == "numerical_linear_solve":
            vector = np.asarray([float(value) for value in (rhs or [])], dtype=float)
            if first_matrix.shape[0] != first_matrix.shape[1] or vector.shape != (first_matrix.shape[0],):
                raise ValueError("数值线性方程需要方阵和匹配的右端向量。")
            result_value: Any = np.linalg.solve(first_matrix, vector)
        elif operation == "numerical_eigenvalues":
            if first_matrix.shape[0] != first_matrix.shape[1]:
                raise ValueError("数值特征值要求方阵。")
            result_value = np.linalg.eigvals(first_matrix)
        else:
            other = _matrix(second_matrix, numeric=True, max_size=200)
            if first_matrix.shape[1] != other.shape[0]:
                raise ValueError("两个数值矩阵的维数不能相乘。")
            result_value = first_matrix @ other
        serializable = np.asarray(result_value).tolist()
        return {
            "operation": operation,
            "result": str(serializable),
            "result_values": serializable,
            "canonical_expression": str(serializable),
            "numeric_approximation": str(serializable),
            "precision": precision,
            "verification": "numpy_numeric",
            "assumptions": list(assumptions or []),
            "domain": str(domain or "unspecified"),
        }

    locals_map, substitutions = _parse_assignments(values)
    locals_map.update(_local_dict(_safe_expression(expression), variable))
    parsed = _parse(expression, locals_map)
    symbol = _symbol(variable, locals_map)
    if operation == "evaluate":
        result = sp.N(parsed.subs(substitutions), precision)
        if result.free_symbols:
            raise ValueError("数值求值后仍有未赋值变量：" + "、".join(sorted(map(str, result.free_symbols))))
    elif operation == "nsolve":
        if len(parsed.free_symbols - {symbol}) > 0:
            raise ValueError("数值求根一次只支持一个未赋值变量。")
        result = sp.nsolve(parsed.subs(substitutions), symbol, float(initial_guess), prec=precision)
    elif operation == "series":
        center = substitutions.get(symbol, sp.Integer(0))
        result = sp.series(parsed, symbol, center, max(2, min(int(order), 30)))
    elif operation == "numerical_integrate":
        if not lower or not upper:
            raise ValueError("数值积分必须提供上下限。")
        result = sp.N(
            sp.Integral(parsed.subs(substitutions), (symbol, _parse(lower, locals_map), _parse(upper, locals_map))),
            precision,
        )
    elif operation == "error_compare":
        if not second_expression:
            raise ValueError("误差比较必须提供 second_expression。")
        second = _parse(second_expression, locals_map)
        left_value = sp.N(parsed.subs(substitutions), precision)
        right_value = sp.N(second.subs(substitutions), precision)
        absolute = abs(left_value - right_value)
        relative = absolute / max(abs(right_value), sp.Float("1e-80"))
        return {
            "operation": operation,
            "expression": str(expression),
            "second_expression": str(second_expression),
            "absolute_error": str(absolute),
            "relative_error": str(relative),
            "result": str(absolute),
            "result_latex": sp.latex(absolute),
            "canonical_expression": str(absolute),
            "numeric_approximation": str(absolute),
            "precision": precision,
            "verification": "sympy_high_precision_numeric",
        }
    else:
        raise ValueError(f"不支持的数值计算操作：{operation}")
    return {
        "operation": operation,
        "expression": str(expression),
        "values": {str(key): str(value) for key, value in (values or {}).items()},
        "result": str(result),
        "result_latex": sp.latex(result),
        "precision": precision,
        "verification": "sympy_high_precision_numeric",
        "canonical_expression": str(result),
        "numeric_approximation": str(result),
        "assumptions": list(assumptions or []),
        "domain": str(domain or "unspecified"),
    }


def _relation(text: str, locals_map: dict[str, Any]) -> tuple[sp.Expr, str, sp.Expr]:
    value = _safe_expression(text)
    match = re.search(r"(<=|>=|==|!=|=|<|>)", value)
    if not match:
        raise ValueError("公式核验或反例搜索需要包含 =、==、!=、<、<=、> 或 >=。")
    left = _parse(value[: match.start()], locals_map)
    right = _parse(value[match.end() :], locals_map)
    return left, match.group(1), right


def _relation_holds(left: complex, operator: str, right: complex, tolerance: float) -> bool:
    if abs(left.imag) > tolerance or abs(right.imag) > tolerance:
        if operator in {"=", "=="}:
            return abs(left - right) <= tolerance
        if operator == "!=":
            return abs(left - right) > tolerance
        return False
    a, b = float(left.real), float(right.real)
    if operator in {"=", "=="}:
        return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))
    if operator == "!=":
        return abs(a - b) > tolerance * max(1.0, abs(a), abs(b))
    return {"<": a < b, "<=": a <= b + tolerance, ">": a > b, ">=": a + tolerance >= b}[operator]


def find_counterexample(
    claim: str,
    *,
    variables: list[str] | None = None,
    ranges: dict[str, list[float]] | None = None,
    samples_per_variable: int = 17,
    tolerance: float = 1e-10,
) -> dict[str, Any]:
    """Search a deterministic bounded grid for a counterexample.

    A failed search is explicitly reported as inconclusive and never as proof.
    """

    names = [str(item) for item in (variables or ["x"])]
    if not names or len(names) > 3 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in names):
        raise ValueError("反例搜索支持一到三个英文变量名。")
    locals_map = {name: sp.Symbol(name, real=True) for name in names}
    left, operator, right = _relation(claim, locals_map)
    count = max(3, min(int(samples_per_variable), 41))
    grids: list[list[float]] = []
    normalized_ranges: dict[str, list[float]] = {}
    for name in names:
        bounds = list((ranges or {}).get(name, [-5.0, 5.0]))
        if len(bounds) != 2 or not all(isfinite(float(item)) for item in bounds):
            raise ValueError(f"变量 {name} 的范围必须是两个有限数。")
        lower, upper = float(bounds[0]), float(bounds[1])
        if lower >= upper:
            raise ValueError(f"变量 {name} 的范围下限必须小于上限。")
        normalized_ranges[name] = [lower, upper]
        grids.append([lower + (upper - lower) * index / (count - 1) for index in range(count)])
    tested = 0
    skipped = 0
    for point in product(*grids):
        substitutions = {locals_map[name]: value for name, value in zip(names, point)}
        try:
            a = complex(sp.N(left.subs(substitutions), 30))
            b = complex(sp.N(right.subs(substitutions), 30))
        except (TypeError, ValueError, ZeroDivisionError):
            skipped += 1
            continue
        if not all(isfinite(item) for item in (a.real, a.imag, b.real, b.imag)):
            skipped += 1
            continue
        tested += 1
        if not _relation_holds(a, operator, b, float(tolerance)):
            return {
                "claim": claim,
                "counterexample_found": True,
                "counterexample": {name: value for name, value in zip(names, point)},
                "left_value": str(a),
                "right_value": str(b),
                "tested_points": tested,
                "skipped_points": skipped,
                "verification": "bounded_numeric_counterexample",
            }
    return {
        "claim": claim,
        "counterexample_found": False,
        "tested_points": tested,
        "skipped_points": skipped,
        "ranges": normalized_ranges,
        "verification": "inconclusive_bounded_search_not_a_proof",
    }


def verify_formula(
    formula: str,
    *,
    variables: list[str] | None = None,
    ranges: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    names = [str(item) for item in (variables or ["x"])]
    locals_map = {name: sp.Symbol(name, real=True) for name in names}
    left, operator, right = _relation(formula, locals_map)
    symbolic_result: bool | None = None
    if operator in {"=", "==", "!="}:
        difference = sp.simplify(left - right)
        symbolic_result = bool(difference == 0)
        if operator == "!=":
            symbolic_result = not symbolic_result if not difference.free_symbols else None
        if symbolic_result is True:
            return {
                "formula": formula,
                "verified": True,
                "method": "symbolic_exact",
                "difference": str(difference),
                "difference_latex": sp.latex(difference),
            }
    counterexample = find_counterexample(formula, variables=names, ranges=ranges)
    if counterexample["counterexample_found"]:
        return {
            "formula": formula,
            "verified": False,
            "method": "counterexample",
            **counterexample,
        }
    return {
        "formula": formula,
        "verified": None,
        "method": "inconclusive_numeric_screening",
        "symbolic_result": symbolic_result,
        "numeric_screening": counterexample,
        "warning": "有限数值采样没有找到反例，但这不构成数学证明。",
    }
