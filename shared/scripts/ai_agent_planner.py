from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.ai_agent_memory import AgentTaskPlan


ROOT_DIR = APP_PATHS.application_root
PLAN_HISTORY_PATH = APP_PATHS.cache_dir / "ai_agent_plan_history.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class PlanStep:
    id: str
    title: str
    allowed_tools: list[str]
    success_evidence: list[str]
    status: str = "pending"
    attempts: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    last_error: str = ""


@dataclass(slots=True)
class AgentExecutionPlan:
    id: str
    created_at: str
    updated_at: str
    task_kind: str
    user_request: str
    context: dict[str, Any]
    state: str
    steps: list[PlanStep]
    replans: list[dict[str, Any]] = field(default_factory=list)
    final_verification: dict[str, Any] = field(default_factory=dict)


def _step(title: str, tools: set[str], evidence: list[str]) -> PlanStep:
    return PlanStep(uuid.uuid4().hex[:10], title, sorted(tools), evidence)


def build_execution_plan(
    task_plan: AgentTaskPlan,
    user_text: str,
    context: dict[str, Any] | None = None,
) -> AgentExecutionPlan:
    read_sources = {
        "semantic_search",
        "list_textbooks",
        "get_textbook_dataset_status",
        "search_textbook_content",
        "resolve_problem_reference",
        "search_problems",
        "get_problem",
        "get_problem_evidence_batch",
        "get_project_problems",
        "list_project_files",
        "read_project_file",
        "read_local_file",
        "read_local_pdf_pages",
        "read_local_pdf_evidence_batch",
        "list_workspace_tree",
        "search_workspace_text",
        "read_workspace_files",
        "inspect_git_changes",
        "inspect_workspace_sqlite",
        "read_workspace_command_log",
        "discover_public_math_resources",
        "search_math_papers",
        "read_math_paper",
        "web_search",
        "fetch_url",
    }
    steps: list[PlanStep] = []
    if task_plan.kind == "account_usage":
        steps.append(
            _step(
                "读取已同步的账户余额与用量",
                {"get_provider_account_usage"},
                ["返回真实余额、用量与更新时间，或明确提示尚未完成网页登录同步"],
            )
        )
    elif task_plan.kind == "paper_research":
        search_step = _step(
            "使用 arXiv 与 Crossref 定位论文版本",
            {"search_math_papers"},
            ["标题、作者、年份、arXiv ID 或 DOI"],
        )
        search_step.status = "required"
        steps.append(search_step)
        read_step = _step(
            "读取最相关论文的摘要或公开正文",
            {"read_math_paper"},
            ["正文页码或明确的仅元数据状态", "可核对的论文链接"],
        )
        read_step.status = "required"
        steps.append(read_step)
    elif task_plan.kind == "lean_formalization":
        steps.append(
            _step(
                "读取原命题、假设与已有 Lean 文件",
                read_sources,
                ["原命题的类型、量词和假设已确认"],
            )
        )
    elif task_plan.kind in {"problem_search", "web_research", "public_resource_discovery", "local_file_task"}:
        steps.append(_step("定位少量候选资料", read_sources, ["返回可核对的来源标识"]))
        steps.append(_step("读取并核对最相关来源", read_sources, ["取得与问题直接相关的正文片段"]))
    elif task_plan.kind in {"drawing_or_visualization", "project_edit"}:
        steps.append(
            _step(
                "读取数学对象、目标文件和现有绘图环境",
                read_sources,
                ["目标路径已读取", "数学定义与坐标已确认"],
            )
        )
    else:
        steps.append(_step("取得当前题目或教材依据", read_sources, ["本地依据已读取或确认无需外部资料"]))

    if task_plan.kind == "lean_formalization":
        if task_plan.write_authorized:
            write_step = _step(
                "在受控目录写入最小 Lean 证明文件",
                {"edit_math_workspace_files"},
                ["事务备份", "Generated 目录中的 .lean 路径"],
            )
            write_step.status = "required"
            steps.append(write_step)
        check_step = _step(
            "调用 Lean 内核核验形式化证明",
            {"lean_check"},
            ["lean_kernel_exit_zero", "无 sorry、admit 或新公理"],
        )
        check_step.status = "required"
        steps.append(check_step)
    elif task_plan.kind == "project_edit":
        steps.append(
            _step(
                "执行一次最小项目修改",
                {
                    "edit_project_tex", "insert_tikz_figure", "edit_math_workspace_files",
                    "apply_workspace_patch", "manage_workspace_files", "run_workspace_sqlite_migration",
                },
                ["备份目录", "项目 PDF 路径、数据库完整性或跨文件事务核验"],
            )
        )
        steps.append(
            _step(
                "核验正式输出并确认没有重复编译",
                {
                    "validate_math_figure", "read_project_file", "read_local_file", "read_workspace_files",
                    "inspect_git_changes", "compile_standalone_tex", "run_workspace_command",
                },
                ["正式产物或事务已核验", "代码命令退出码、视觉、数据库或文件证据通过"],
            )
        )
    elif task_plan.kind == "drawing_or_visualization":
        steps.append(
            _step(
                "生成并检查数学图形",
                {"symbolic_math", "numerical_math", "verify_formula", "find_counterexample", "mathematica_compute", "mathematica_plot", "dual_verify_math", "plot_math_function", "render_math_figure_preview", "validate_math_figure"},
                ["公式核验", "视觉检查报告"],
            )
        )
    elif task_plan.write_authorized:
        steps.append(
            _step(
                "执行仓库修改并保留可恢复证据",
                {
                    "apply_workspace_patch",
                    "manage_workspace_files",
                    "run_workspace_sqlite_migration",
                    "rebind_textbook_pdf",
                    "import_vocabulary_entries",
                    "set_vocabulary_familiarity",
                    "delete_vocabulary_entries",
                },
                ["备份或回收路径", "逐文件哈希回读或数据库完整性检查"],
            )
        )
        steps.append(
            _step(
                "运行与修改风险相称的本地验证",
                {"run_workspace_command", "inspect_git_changes", "read_workspace_files", "inspect_workspace_sqlite"},
                ["退出码为 0 或正式目标回读核验通过", "本轮改动范围可核对"],
            )
        )
    elif task_plan.kind not in {"account_usage", "lean_formalization", "paper_research"}:
        steps.append(
            _step(
                "核验关键数学计算或逻辑条件",
                {"symbolic_math", "numerical_math", "verify_formula", "find_counterexample", "mathematica_compute", "dual_verify_math"},
                ["精确或数值核验结果；若不需要应明确跳过"],
            )
        )
    steps.append(_step("形成回答并按验收标准自检", set(), list(task_plan.success_criteria)))
    now = _now()
    return AgentExecutionPlan(
        id=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        task_kind=task_plan.kind,
        user_request=str(user_text or "")[:4000],
        context={
            key: value
            for key, value in dict(context or {}).items()
            if key in {"subject_name", "project_ref", "problem_ref", "page_name"}
        },
        state="planned",
        steps=steps,
    )


class AgentPlannerStateMachine:
    """Persistent plan -> execute -> verify -> replan state machine.

    Provider tool loops remain responsible for choosing exact arguments.  This
    state machine constrains that loop, records evidence and sends a concrete
    recovery instruction back to the model after a failed tool call.
    """

    def __init__(self, plan: AgentExecutionPlan, history_path: Path = PLAN_HISTORY_PATH) -> None:
        self.plan = plan
        self.history_path = Path(history_path)
        self._save()

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan.id,
            "state": self.plan.state,
            "steps": [
                {
                    "id": step.id,
                    "title": step.title,
                    "allowed_tools": step.allowed_tools,
                    "success_evidence": step.success_evidence,
                    "status": step.status,
                }
                for step in self.plan.steps
            ],
            "rule": "按顺序执行；工具失败时读取 planner_guidance 后重规划，不得原样重复失败调用。",
        }

    def _active_step(self, tool_name: str = "") -> PlanStep | None:
        pending = [
            step for step in self.plan.steps if step.status in {"pending", "required", "in_progress"}
        ]
        if not pending:
            return None
        if tool_name:
            matching = [step for step in pending if tool_name in step.allowed_tools]
            if matching:
                return matching[0]
        return pending[0]

    def before_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        step = self._active_step(tool_name)
        self.plan.state = "executing"
        self.plan.updated_at = _now()
        if step is not None:
            step.status = "in_progress"
            step.attempts += 1
        self._save()
        return {
            "plan_id": self.plan.id,
            "state": self.plan.state,
            "step_id": step.id if step else "",
            "step_title": step.title if step else "",
            "tool": tool_name,
            "arguments": arguments,
        }

    def after_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        step = self._active_step(tool_name)
        call = {
            "created_at": _now(),
            "tool": tool_name,
            "arguments": arguments,
            "ok": bool(result.get("ok")),
            "summary": str(result.get("error") or "工具返回成功")[:1000],
        }
        if step is not None:
            step.tool_calls.append(call)
        if result.get("ok"):
            if step is not None:
                step.status = "completed"
                step.last_error = ""
                if step.title.startswith("失败恢复："):
                    original = next(
                        (item for item in self.plan.steps if item.status == "replanning"), None
                    )
                    if original is not None:
                        original.status = "replanned"
            self.plan.state = "verifying"
            guidance = "核对该结果是否满足本步骤证据；满足则进入下一步，不要重复调用同一工具。"
        else:
            if step is not None:
                step.status = "replanning"
                step.last_error = str(result.get("error") or "工具执行失败")[:1000]
            self.plan.state = "replanning"
            recovery_tools = sorted(
                {
                    "semantic_search",
                    "list_project_files",
                    "read_project_file",
                    "read_local_file",
                    "symbolic_math",
                    "mathematica_compute",
                    "mathematica_plot",
                    "dual_verify_math",
                    "plot_math_function",
                    "verify_formula",
                }
                & {tool for plan_step in self.plan.steps for tool in plan_step.allowed_tools}
            )
            replan = {
                "created_at": _now(),
                "failed_tool": tool_name,
                "error": str(result.get("error") or "")[:1000],
                "recovery_tools": recovery_tools,
                "instruction": "不要原样重复失败调用；补充缺失资料、缩小操作或改用只读核验后再决定是否继续。",
            }
            self.plan.replans.append(replan)
            recovery_step = _step(
                "失败恢复：补充证据或缩小操作",
                set(recovery_tools),
                ["替代工具成功返回，或明确确认任务无法继续"],
            )
            retry_step = _step(
                "按重规划后的参数重试原步骤",
                set(step.allowed_tools if step is not None else []),
                list(step.success_evidence if step is not None else []),
            )
            if tool_name in {
                "edit_project_tex",
                "insert_tikz_figure",
                "build_project_pdf",
                "edit_math_workspace_files",
                "compile_standalone_tex",
            }:
                retry_step.status = "required"
            if step is not None:
                insert_at = self.plan.steps.index(step) + 1
                self.plan.steps.insert(insert_at, recovery_step)
                self.plan.steps.insert(insert_at + 1, retry_step)
            else:
                self.plan.steps.insert(max(0, len(self.plan.steps) - 1), recovery_step)
            guidance = replan["instruction"] + (" 可用恢复工具：" + "、".join(recovery_tools) if recovery_tools else "")
        self.plan.updated_at = _now()
        self._save()
        return {
            "plan_id": self.plan.id,
            "state": self.plan.state,
            "step_id": step.id if step else "",
            "planner_guidance": guidance,
        }

    def finalize(
        self,
        *,
        execution_verification: dict[str, Any],
        quality_report: dict[str, Any],
        answer_present: bool,
    ) -> dict[str, Any]:
        self.plan.state = "verifying"
        final_step = self.plan.steps[-1] if self.plan.steps else None
        verified = bool(execution_verification.get("all_verified", True))
        quality_passed = bool(quality_report.get("passed", True))
        unresolved = [
            step
            for step in self.plan.steps[:-1]
            if step.status in {"failed", "replanning", "required", "in_progress"}
        ]
        for step in self.plan.steps[:-1]:
            if step.status == "pending":
                step.status = "skipped"
        completed = bool(answer_present and verified and quality_passed and not unresolved)
        if final_step is not None:
            final_step.status = "completed" if completed else "failed"
        self.plan.state = "completed" if completed else "failed"
        self.plan.final_verification = {
            "answer_present": bool(answer_present),
            "execution_verified": verified,
            "quality_passed": quality_passed,
            "issues": quality_report.get("issues", []),
        }
        self.plan.updated_at = _now()
        self._save()
        return {
            "plan_id": self.plan.id,
            "state": self.plan.state,
            "task_kind": self.plan.task_kind,
            "step_count": len(self.plan.steps),
            "completed_steps": sum(
                step.status in {"completed", "replanned", "skipped"} for step in self.plan.steps
            ),
            "failed_steps": [
                step.title
                for step in self.plan.steps
                if step.status in {"failed", "replanning", "required", "in_progress"}
            ],
            "replan_count": len(self.plan.replans),
            "final_verification": self.plan.final_verification,
        }

    def _save(self) -> None:
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = []
        records = [item for item in raw if isinstance(item, dict) and item.get("id") != self.plan.id]
        records.append(asdict(self.plan))
        records = records[-100:]
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.history_path)
