from __future__ import annotations

import json
import sys
from typing import Any

from shared.scripts.ai_agent_math_tools import (
    find_counterexample,
    numerical_math,
    symbolic_math,
    verify_formula,
)


def _run(tool: str, arguments: dict[str, Any], artifact_dir: str = "") -> dict[str, Any]:
    if tool == "symbolic_math":
        return symbolic_math(**arguments)
    if tool == "numerical_math":
        return numerical_math(**arguments)
    if tool == "verify_formula":
        return verify_formula(**arguments)
    if tool == "find_counterexample":
        return find_counterexample(**arguments)
    if tool == "plot_math_function":
        if not artifact_dir:
            raise ValueError("绘图 worker 缺少受信任的产物目录。")
        from shared.scripts.ai_agent_plot_tools import plot_math_function

        return plot_math_function(artifact_dir=artifact_dir, **arguments)
    raise ValueError(f"不支持的固定计算工具：{tool}")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("计算请求必须是 JSON 对象。")
        tool = str(request.get("tool") or "")
        arguments = dict(request.get("arguments") or {})
        result = _run(tool, arguments, str(request.get("artifact_dir") or ""))
        sys.stdout.write(json.dumps({"success": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as error:
        sys.stdout.write(
            json.dumps(
                {"success": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
