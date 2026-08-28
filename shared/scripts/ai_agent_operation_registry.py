from __future__ import annotations

import hashlib
import json
from collections.abc import Set
from dataclasses import dataclass
from typing import Any, Iterable


READ_ONLY = "read_only"
DERIVED_WRITE = "derived_write"
FORMAL_WRITE = "formal_write"
DESTRUCTIVE = "destructive"
ACCESS_LEVELS = {READ_ONLY, DERIVED_WRITE, FORMAL_WRITE, DESTRUCTIVE}

# These names remain visible in the capability catalogue even when the current
# turn has not authorized formal writes.  Authorization controls execution, not
# discoverability.
_HISTORIC_FORMAL_WRITE_TOOL_NAMES = frozenset(
    {
        "edit_project_tex",
        "insert_tikz_figure",
        "build_project_pdf",
        "edit_math_workspace_files",
        "compile_standalone_tex",
        "apply_workspace_patch",
        "manage_workspace_files",
        "run_workspace_command",
        "run_workspace_sqlite_migration",
        "rebind_textbook_pdf",
    }
)


class _RegisteredFormalWriteToolNames(Set[str]):
    """Live view of every formal/destructive tool, including future specs."""

    @staticmethod
    def _values() -> set[str]:
        values = set(_HISTORIC_FORMAL_WRITE_TOOL_NAMES)
        registry = globals().get("AI_OPERATION_REGISTRY")
        if registry is not None:
            values.update(
                spec.tool_name
                for spec in registry._specs.values()
                if spec.access_level in {FORMAL_WRITE, DESTRUCTIVE}
            )
        return values

    def __contains__(self, value: object) -> bool:
        return str(value or "") in self._values()

    def __iter__(self):
        return iter(sorted(self._values()))

    def __len__(self) -> int:
        return len(self._values())


FORMAL_WRITE_TOOL_NAMES: Set[str] = _RegisteredFormalWriteToolNames()


@dataclass(frozen=True, slots=True)
class OperationSpec:
    operation_id: str
    tool_name: str
    description: str
    parameters: dict[str, Any]
    handler_name: str
    access_level: str = READ_ONLY
    category: str = "general"
    evidence_policy: str = "result_readback"
    ai_visibility: str = "exposed"

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": self.description,
            "parameters": self.parameters,
        }


class OperationRegistry:
    """Single capability catalogue shared by the model, executor, UI and tests.

    The historic tool list is locked by a name-manifest hash.  New tools cannot
    be smuggled into that legacy list: they must be explicit ``OperationSpec``
    entries with a real executor handler, or registry validation fails.
    """

    def __init__(self) -> None:
        self._legacy_definitions: list[dict[str, Any]] = []
        self._legacy_names: set[str] = set()
        self._specs: dict[str, OperationSpec] = {}
        self._operation_ids: set[str] = set()

    @staticmethod
    def manifest_hash(names: Iterable[str]) -> str:
        payload = json.dumps(sorted(str(name) for name in names), separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def register_legacy_definitions(
        self,
        definitions: Iterable[dict[str, Any]],
        *,
        expected_manifest_hash: str,
    ) -> None:
        if self._legacy_definitions:
            raise RuntimeError("旧 AI 工具清单已经注册，不能重复初始化。")
        copied = [dict(item) for item in definitions]
        names = [str(item.get("name") or "").strip() for item in copied]
        if not names or any(not name for name in names):
            raise RuntimeError("旧 AI 工具清单包含空名称。")
        if len(names) != len(set(names)):
            raise RuntimeError("旧 AI 工具清单包含重复名称。")
        actual_hash = self.manifest_hash(names)
        if actual_hash != str(expected_manifest_hash):
            raise RuntimeError(
                "旧 AI 工具清单发生变化。新能力必须通过 OperationSpec 注册，"
                f"不能直接塞入旧清单（expected={expected_manifest_hash}, actual={actual_hash}）。"
            )
        self._legacy_definitions = copied
        self._legacy_names = set(names)

    def register(self, spec: OperationSpec) -> None:
        if spec.access_level not in ACCESS_LEVELS:
            raise ValueError(f"未知 AI 工具权限等级：{spec.access_level}")
        if spec.ai_visibility != "exposed":
            raise ValueError("可复用应用操作默认必须面向 AI；纯界面行为不应注册为工具。")
        if not spec.operation_id or not spec.tool_name or not spec.handler_name:
            raise ValueError("AI 操作必须同时提供 operation_id、tool_name 和 handler_name。")
        if spec.tool_name in self._legacy_names or spec.tool_name in self._specs:
            raise ValueError(f"AI 工具名称重复：{spec.tool_name}")
        if spec.operation_id in self._operation_ids:
            raise ValueError(f"AI 操作编号重复：{spec.operation_id}")
        if spec.parameters.get("type") != "object":
            raise ValueError(f"AI 工具参数必须使用 object schema：{spec.tool_name}")
        self._specs[spec.tool_name] = spec
        self._operation_ids.add(spec.operation_id)

    def definitions(self) -> list[dict[str, Any]]:
        return [
            *[dict(item) for item in self._legacy_definitions],
            *[spec.definition() for spec in self._specs.values()],
        ]

    def spec(self, tool_name: str) -> OperationSpec | None:
        return self._specs.get(str(tool_name or ""))

    def access_level(self, tool_name: str) -> str:
        spec = self.spec(tool_name)
        if spec is not None:
            return spec.access_level
        if tool_name in FORMAL_WRITE_TOOL_NAMES:
            return FORMAL_WRITE
        return READ_ONLY

    def catalog(self) -> list[dict[str, Any]]:
        descriptions = {
            str(item.get("name") or ""): str(item.get("description") or "")
            for item in self.definitions()
        }
        rows: list[dict[str, Any]] = []
        for name in sorted(descriptions):
            spec = self.spec(name)
            rows.append(
                {
                    "name": name,
                    "operation_id": spec.operation_id if spec else f"legacy.{name}",
                    "category": spec.category if spec else "legacy",
                    "access": self.access_level(name),
                    "ai_visibility": "exposed",
                    "description": descriptions[name],
                    "evidence_policy": spec.evidence_policy if spec else "result_readback",
                    "registered_handler": spec.handler_name if spec else "legacy_executor",
                }
            )
        return rows

    def validate(self, executor_type: type[Any]) -> list[str]:
        errors: list[str] = []
        definitions = self.definitions()
        names = [str(item.get("name") or "") for item in definitions]
        if len(names) != len(set(names)):
            errors.append("工具定义存在重复名称")
        for definition in definitions:
            name = str(definition.get("name") or "")
            if not name:
                errors.append("工具定义存在空名称")
            if not str(definition.get("description") or "").strip():
                errors.append(f"工具缺少 description：{name}")
            parameters = definition.get("parameters")
            if not isinstance(parameters, dict) or parameters.get("type") != "object":
                errors.append(f"工具缺少 object 参数结构：{name}")
        for spec in self._specs.values():
            handler = getattr(executor_type, spec.handler_name, None)
            if not callable(handler):
                errors.append(
                    f"注册工具缺少执行器方法：{spec.tool_name} -> {spec.handler_name}"
                )
        return errors


AI_OPERATION_REGISTRY = OperationRegistry()
