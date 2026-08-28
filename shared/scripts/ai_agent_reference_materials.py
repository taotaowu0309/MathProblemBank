from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
QT_SOURCE = ROOT_DIR / "shared" / "scripts" / "problem_bank_center_qt.py"

REFERENCE_MATERIALS: dict[str, dict[str, str]] = {
    "latex_writing_rules": {
        "title": "LaTeX 写作与题目导入规范",
        "kind": "file",
        "source": "shared/templates/latex_writing_rules.txt",
        "purpose": "直接导入、批量导入、PDF 标签和 Problem Summary 的唯一可编辑规范",
    },
    "direct_import_template_chinese": {
        "title": "直接导入题目中文模板",
        "kind": "python_constant",
        "source": "DIRECT_IMPORT_CHINESE_TEMPLATE",
        "purpose": "单题中文字段、LaTeX 与词汇表导入格式示例",
    },
    "direct_import_template_english": {
        "title": "Direct problem import English template",
        "kind": "python_constant",
        "source": "DIRECT_IMPORT_ENGLISH_TEMPLATE",
        "purpose": "English field names, LaTeX rules and vocabulary section format",
    },
    "direct_import_template_batch": {
        "title": "批量直接导入题目模板",
        "kind": "python_constant",
        "source": "DIRECT_IMPORT_BATCH_TEMPLATE",
        "purpose": "多题批量边界、字段顺序和词汇表格式",
    },
    "background_import_prompt": {
        "title": "背景图导入 Codex 提示词",
        "kind": "file",
        "source": "shared/templates/background_import_codex_prompt.txt",
        "purpose": "背景图尺寸、色彩、质量与回归要求",
    },
    "ai_tool_workflows": {
        "title": "AI 数学工具工作流",
        "kind": "file",
        "source": "shared/templates/ai_agent_training/tool_workflows.md",
        "purpose": "AI 使用题库、教材、本地文件与工具的流程说明",
    },
    "ai_math_style_guide": {
        "title": "AI 数学回答风格规范",
        "kind": "file",
        "source": "shared/templates/ai_agent_training/math_style_guide.md",
        "purpose": "数学解释、证明与排版风格",
    },
    "ai_physics_tool_workflows": {
        "title": "AI 物理工具工作流",
        "kind": "file",
        "source": "shared/templates/ai_agent_training/physics_tool_workflows.md",
        "purpose": "物理助手的本地资料、计算与验证流程",
    },
    "ai_portable_skill_profiles": {
        "title": "AI 可移植 Skill 兼容训练配置",
        "kind": "file",
        "source": "shared/templates/ai_agent_training/portable_skill_profiles.json",
        "purpose": "把计算、PDF 检索和个人知识管理 Skill 映射到现有正式 AI 工具",
    },
}


def _python_string_constants() -> dict[str, str]:
    source = QT_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(QT_SOURCE))
    values: dict[str, str] = {}
    wanted = {
        item["source"]
        for item in REFERENCE_MATERIALS.values()
        if item["kind"] == "python_constant"
    }
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not wanted.intersection(names):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            for name in names:
                if name in wanted:
                    values[name] = value
    return values


class AiReferenceMaterialRegistry:
    """Stable, read-only IDs for project rules and embedded import templates."""

    def list_materials(self) -> dict[str, Any]:
        return {
            "materials": [
                {
                    "material_id": material_id,
                    "title": item["title"],
                    "purpose": item["purpose"],
                    "source_kind": item["kind"],
                    "source_locator": item["source"],
                }
                for material_id, item in REFERENCE_MATERIALS.items()
            ],
            "count": len(REFERENCE_MATERIALS),
            "read_tool": "read_ai_reference_material",
        }

    def read_material(self, material_id: str) -> dict[str, Any]:
        key = str(material_id or "").strip()
        if key not in REFERENCE_MATERIALS:
            raise ValueError(
                "未知资料 ID。请先调用 list_ai_reference_materials 获取稳定目录。"
            )
        item = REFERENCE_MATERIALS[key]
        if item["kind"] == "file":
            path = (ROOT_DIR / item["source"]).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"规范资料不存在：{path}")
            content = path.read_text(encoding="utf-8-sig")
            source_path = str(path)
            source_locator = item["source"]
        else:
            constants = _python_string_constants()
            constant_name = item["source"]
            if constant_name not in constants:
                raise RuntimeError(f"无法从正式 Qt 实现读取模板常量：{constant_name}")
            content = constants[constant_name]
            source_path = str(QT_SOURCE.resolve())
            source_locator = f"shared/scripts/problem_bank_center_qt.py::{constant_name}"
        return {
            "material_id": key,
            "title": item["title"],
            "purpose": item["purpose"],
            "content": content,
            "character_count": len(content),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "source_path": source_path,
            "source_locator": source_locator,
            "read_only": True,
        }
